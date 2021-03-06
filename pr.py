"""
pr.py -- phasereads.py

hapcut.HapCut() takes a hapcut file and returns a dictionary of
refnames and a list of hapcut.Blocks. Each hapcut.Block contains
block info and a list of block entries (phased variants).

Notes:

 - I use custom exceptions to handle specific types of issues in
   reads.

TODO:
 - reads shared across blocks
 - read accounting
 - checking that mates are not needlessly thrown away
"""
TEST = True
import pdb
import time
import sys
from multiprocessing import Queue, Pool, Process
import argparse
from collections import defaultdict, OrderedDict, Counter
from operator import itemgetter
import pysam
from Bio.Seq import Seq
from hapcut import HapCut
#import fermi as fm
from readset import ReadSet

### Read-level Exceptions ###
#
# These cleanup the core calls to count_read_haplotypes() by allow
# exceptions to be raised and handled at higher levels (rather than
# through ugly return values).

def has_indel(read):
    return any([op in (1, 2) for op, _ in read.cigar])

def filter_fun_factory(mapq, exclude_duplicates, exclude_indels):
    """
    Make a closure around some filtering options. This returns a tuple
    of a counter, and the filtering closure. Side-effects of the
    counter occur while filter occurs.

    The stats key "filtered" is updated, which allows total counts to kept. Otherwise, reads being
    filtered for more than one reason may be double counted.
    """
    stats = Counter()
    def read_passes_fun(read):
        stats["total"] += 1
        if read.is_unmapped:
            stats["unmapped"] += 1
            return False
        filters_fail = {"mapq<%d" % mapq:read.mapq < mapq,
                        "indels":exclude_indels and read.cigar is not None and has_indel(read),
                        "duplicates":exclude_duplicates and read.is_duplicate}
        failed = False
        for k, v in filters_fail.items():
            failed = failed or v
            stats[k] += v
        stats["filtered"] += failed
            
        return not any(filters_fail.values())
    return stats, read_passes_fun

def hashreads(readiter, bamfile, pass_filter_fun):
    """
    Reads are processed at the fragment level (both reads in the
    pair). This hashes them all first for quicker access, as seeking
    via file (via pysam.Samfile.getmate()) is very slow. filter_fun()
    is a function used for filtering, should return True if a read
    passes a filter.

    Because this fetches my reference, proper-pairs (based on common
    reference mapping, not insert size) are only here.
    """
    reads = defaultdict(lambda: [None, None])
    for read in readiter:
        if not pass_filter_fun(read):
            continue
        which_read = 0 if read.is_read1 else 1
        reads[read.qname][which_read] = read
    return reads

def revcomp(seq):
    return str(Seq(seq).reverse_complement())

def print_block_stats(refname, block_id, allele_counts, stats):
    """
    For each block, print allele counts and the read statistics.
    """
    stats_str = " ".join("%s=%s" % (k, v) for k, v in stats.items())
    line_fmt = "# contig='%s' block_id=%d " % (refname, block_id)
    line_fmt += stats_str + "\n"
    sys.stdout.write(line_fmt)
    # we sort so keys are consistently ordered
    sorted_counts = sorted(allele_counts.items(), key=itemgetter(0))
    for pos, counts in sorted_counts:
        joined = ";".join(["%s:%s" % (a, c) for a, c in counts.items()])
        sys.stdout.write("%s\t%d\t%d\t%s\n" % (refname, block_id, pos, joined))
    sys.stdout.flush()

def get_block_haplotypes(block):
    """
    Given a block from HapCut's Block named tuple, make a dictionary
    of all variants and positions. Keys are typles of:
     - start position
     - end position
     - length
    """
    haplotypes = dict()
    for entry in block.entries:
        ref_key = (entry.ref_allele, entry.haplotype_1)
        var_key = (entry.var_allele, entry.haplotype_2)
        allele_len = len(entry.ref_allele)
        allele_tup = (entry.position-1, entry.position-1 + allele_len, allele_len)
        haplotypes[allele_tup] = dict([ref_key, var_key])
    return haplotypes

def group_varpos_by_haplotype(readpair, haplotypes, allele_counts):
    """
    Given a readpair tuple (entire paired fragment), compare each
    variant position from the HapCut haplotype data to the variant
    present in the read. Variant interval tuples will be grouped into
    a list per haplotype; both haplotypes are stored in dictionary
    with keys as [0, 1, None]. None indicates that the read contained
    an variant at the position that was not phased, either due to
    sequencing or mapping error, triallelic variant. Indel-containing
    reads are not phased.

    Not that read pairs *could* overlap, and one could have a
    sequencing error at this same position, so the same interval could
    be in two haplotype lists.
    """
    htypes_pos = defaultdict(list)
    for read in readpair:
        if read is None:
            continue
        assert(not has_indel(read))
        for key, alleles in haplotypes.items():
            # alleles is dict of allele:phase
            interval = key[0:2]
            allele_len = key[2]
            if read.get_overlap(interval[0], interval[1]) == allele_len:
                read_var = read.query[interval[0]-read.pos:interval[1]-read.pos]
                htype = alleles.get(read_var, None)
                allele_counts[interval[0]][read_var] += 1
                htypes_pos[htype].append(interval)
    return htypes_pos

def has_inconsistent_overlap(htype_pos):
    """
    Given haplotype position lists, return boolean whether haplotype
    has read pairs that overlap (isize < 0), and have two different
    alleles at a position (which must be due to error).
    """
    all_intervals = list()
    for intervals in htype_pos.values():
        all_intervals.extend(list(set(intervals)))
    return any(map(lambda x: x > 1, Counter(all_intervals).values()))


def is_inconsistent_read(htype_pos):
    """
    Given haplotype positions list, return whether haplotype is
    inconsistent.
    """
    return len(set(htype_pos.keys())) > 1

def minor_haplotype_position(htype_pos):
    """
    Return the position of the the least abundant haplotype in a read
    with inconsistent haplotypes.

    Note: this does not handle ties, which could occur if two variants
    are out of phase and have reads cross them. Since there are only
    two, it is impossible to indentify which is out of phase.
    """
    assert(not has_inconsistent_overlap(htype_pos))
    assert(is_inconsistent_read(htype_pos))
    minor_htype = sorted(htype_pos.items(), key=lambda x: len(x[1]))[0]
    return minor_htype[1]
    
def group_reads_by_block(reads, block, block_id, callback, stats, inconsistent_counts_file=None):
    """
    group_reads_by_block() takes a dictionary of reads by read name,
    with the values as lists of length two, of each pair (or None for
    missing). It also takes a HapCut.Block (a single phased block) and
    group's the reads by phased variants.
    """
    refname = block.entries[0].chromosome
    sys.stderr.write("[phase_reads] phasing '%s', block_id %d\n" % (refname, block_id))
    sys.stderr.flush()

    # make a dictionary of all variants in a phased block
    haplotypes = get_block_haplotypes(block)

    # initiate data structures and counters for this block
    phased_readsets = (ReadSet(CT=refname, BL=block_id, PH=0), ReadSet(CT=refname, BL=block_id, PH=1))
    unused_readset = ReadSet(CT=refname, BL="NA", PH="NA")
    allele_counts = defaultdict(Counter)
    inconsistent_minor_htype = Counter()
    block_stats = Counter()
    
    for qname, readpair in reads.items():
        # number of reads in this readpair/fragment - necessary for read accounting
        numreads = sum(int(r is not None) for r in readpair)
        
        htype_pos = group_varpos_by_haplotype(readpair, haplotypes, allele_counts)
        if not len(htype_pos):
            block_stats["no_overlap"] += numreads
            # this read doesn't overlap a variant
            continue
        if has_inconsistent_overlap(htype_pos):
            block_stats["inconsistent_overlap"] += numreads
            unused_readset.add_readpair(readpair)
            continue
        if not has_inconsistent_overlap(htype_pos) and is_inconsistent_read(htype_pos):
            # Grab most common out of phase allele. Our power here is
            # greatly linked to how many close variants we have: if we
            # have 5 variants in a fragement that are in phase, and 1
            # out of phase (and this same out of phase is the same in
            # many) we can detect it. Two variants disagreeing in
            # phase in a majority of reads don't allow us to infer
            # which is out of phase.
            block_stats["inconsistent_phase"] += numreads
            minor_htype_pos = minor_haplotype_position(htype_pos)
            for pos in minor_htype_pos:
                inconsistent_minor_htype[pos] += 1
            unused_readset.add_readpair(readpair)
            continue
        if len(htype_pos) == 1 and htype_pos.keys()[0] is None:
            # this read's only overlap with a variant is not a phased
            # allele, so the key is None. Add to unused.
            block_stats["unphased_allele"] += numreads
            unused_readset.add_readpair(readpair)
            continue
        assert(len(htype_pos) == 1)
        phase = htype_pos.keys()[0]
        block_stats['phased'] += numreads
        phased_readsets[phase].add_readpair(readpair)

    try:
        # here, we keep track of our total reads in and out, for
        # different reasons.
        sumkeys = ("filtered", "unmapped", "phased", "unphased_allele",
                   "inconsistent_phase", "no_overlap", "inconsistent_overlap")
        all_stats = block_stats + stats
        processed = dict((k, all_stats[k]) for k in sumkeys)
        assert(stats["total"] == sum(processed.values()))
    except AssertionError:
        sys.stderr.write("[error] inconsistent totals for contig %s" % refname)

    if inconsistent_counts_file is not None:
        for pos, count in inconsistent_minor_htype.items():
            inconsistent_counts_file.write("\t".join(map(str, (refname, block_id, pos, count))) + "\n")
    print_block_stats(refname, block_id, allele_counts, all_stats)
    callback(phased_readsets, unused_readset)


def phase_reads(bam_filename, hapcut_file, unphased_file, inconsistent_counts_file,
                mapq, exclude_duplicates, callback, region=None):
    """
    phase_reads() is the primary function that dispatches
    group_reads_by_block() per block. Given a BAM filebame, HapCut
    file, and unphased_file for contigs that have not been phased (not
    in HapCut's output) this will call group_reads_by_block() and
    phase blocks. Results are passed as a tuple (phased_readset, and
    unused_readset) to a callback function. Optionally, a region can
    be provided.

    mapq and exclude_duplicates are for filtering reads used in
    phasing, and should match the options chosen by HapCut and
    FreeBayes.

    As an aside, a callback is used so that if further processing is
    needed (like with the assembly option), it can be done
    asynchronously in another process (or many processes). A callback
    also allows a universal phase_reads() function for the 'assemble'
    and 'output' subcommands.
    """
    sys.stderr.write("[phase_reads] opening alignment BAM file...\t")
    bamfile = pysam.Samfile(bam_filename, 'rb')
    sys.stderr.write("done.\n")

    hapcut_dict = HapCut(hapcut_file).to_dict()

    if region is not None:
        hapcut_dict = dict([(region, hapcut_dict[region])])

    for refname, phased_blocks in hapcut_dict.iteritems():
        stats, filter_fun = filter_fun_factory(mapq, exclude_duplicates, exclude_indels=True)
        reads = hashreads(bamfile.fetch(reference=refname), bamfile, filter_fun)
        for block_id, block in enumerate(phased_blocks):
            group_reads_by_block(reads, block, block_id, callback, stats, inconsistent_counts_file)

    # handle unphased contigs
    if unphased_file is not None:
        unphased_contigs = set(bamfile.references) - set(hapcut_dict.keys())
        for read in bamfile:
            if (read.is_unmapped or read.mapq < mapq or (exclude_duplicates and read.is_duplicate)):
                continue
            readname = bamfile.getrname(read.tid)
            if readname not in unphased_contigs:
                continue
            which_read = 1 if read.is_read1 else 2
            seq = read.query
            qual = read.qual
            if read.is_reverse:
                seq = revcomp(seq)
                qual = qual[::-1]
            fields = map(str, (read.qname, which_read, bamfile.getrname(read.tid)))
            # NP: not phased
            header = "%s-%s CT:%s BL:NP PH:NP" % tuple(fields)
            unphased_file.write("@%s\n%s\n+\n%s\n" % (header, seq, qual))

def output_main(args):
    """
    main() function for outputting reads to a file. This also defines
    a closure callback function over some of the arguments.
    """
    
    def writer_callback(phased_readset, unused_readset):
        if args.phased is not None:
            phased_readset[0].write(args.phased)
            phased_readset[1].write(args.phased)
        if args.unused_phased is not None:
            unused_readset.write(args.unused_phased)

    phase_reads(args.bam, args.hapcut, args.unphased, args.inconsistent_counts,
                args.mapq, args.exclude_duplicates, writer_callback,
                region=args.region)

if __name__ == "__main__":
    msg = "divide reads into groups, based on HapCut phasing results"
    parser = argparse.ArgumentParser(description=msg)
    parser.add_argument("-u", "--unphased",
                        help="FASTA filename for reads from unphased contigs",
                        type=argparse.FileType('w'), default=None, required=False)
    parser.add_argument("-p", "--phased",
                        help="FASTA filename for reads from phased contigs",
                        type=argparse.FileType('w'), default=None)
    parser.add_argument("-o", "--unused-phased",
                        help="FASTA filename for reads from phased "
                        "contigs unused during phasing",
                        type=argparse.FileType('w'), default=None)
    parser.add_argument("-i", "--inconsistent-counts",
                        help="file to write contigs with inconsistently phased alleles (tab format)",
                        type=argparse.FileType('w'), default=None, required=False)    
    parser.add_argument("-m", "--mapq",
                        help="mapping quality threshold (exclude if below)", 
                        type=int, required=False, default=0)
    parser.add_argument("-d", "--exclude-duplicates", help="exclude duplicate reads", 
                        action="store_true", default=True)
    parser.add_argument("hapcut", help="hapcut file", default=None,
                        type=argparse.FileType('r'))
    parser.add_argument("bam", help="BAM file of aligned reads", default=None,
                        type=str)
    parser.add_argument("region", help="optional region", default=None,
                        type=str, nargs="?")
    args = parser.parse_args()
    output_main(args)
