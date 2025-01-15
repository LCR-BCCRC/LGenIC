#!/usr/bin/env python

# Created by Christopher Rushton 2020/04/20
# This converts a MAF file and copy number matrix (optional) into the input files used be the LymphGen lymphoma
# classifier. See https://llmpp.nih.gov/lymphgen/index.php for more information

import argparse
import math
import os
import bisect
import logging
from collections import Counter
from sortedcontainers import SortedSet


class Gene:
    """
    A simple class storing the chromosome, start, end, and name of a gene
    """
    __slots__ = ["name", "chrom", "start", "end"]

    def __init__(self, chrom, start, end, name):
        self.name = name
        self.chrom = chrom
        self.start = start
        self.end = end


class Chromosome:
    """
    A simple class storying the genomic range of each chromosome, as well as the coordiates of the p and q arm
    """

    def __init__(self, chrom):
        self.chrom = chrom
        self.p_start = None
        self.p_end = None
        self.q_start = None
        self.q_end = None
        self.q_length = 0.0
        self.p_length = 0.0

    def add(self, start, end, arm):

        if arm == "p":
            # Check to see if we already have genomic coordinates for this arm
            if self.p_start is not None or self.p_end is not None:
                raise AttributeError("Coordinates already assigned for %s arm of chromosome \'%s\'" % (arm, self.chrom))
            self.p_start = start
            self.p_end = end
            self.p_length = end - start

            # Sanity check this does not overlap the q arm
            if self.q_start is not None and self.p_end > self.q_start:
                raise AttributeError("For chromosome \'%s\'. The q arm starts before the end of the p arm" % self.chrom)

        elif arm == "q":
            # Check to see if we already have genomic coordinates for this arm
            if self.q_start is not None or self.q_end is not None:
                raise AttributeError("Coordinates already assigned for %s arm of chromosome \'%s\'" % (arm, self.chrom))
            self.q_start = start
            self.q_end = end
            self.q_length = end - start

            # Sanity check this does not overlap the p arm
            if self.p_end is not None and self.q_start < self.p_end:
                raise AttributeError("For chromosome \'%s\'. The q arm starts before the end of the p arm" % self.chrom)
        else:
            # Not the p or q arm. Invalid
            raise AttributeError("Unknown arm specified for chromosome \'%s\':\'%s\'" % (self.chrom, arm))


class SampleCNVs:
    """
    Stores all the copy number events within a given class
    I was going to write this in a function, but having a class is a lot cleaner
    """

    def __init__(self):
        self.starts = {}
        self.ends = {}
        self.cn_states = {}
        self.len_seg = {}
        self.is_chr_prefixed = None
        self.ploidy = None

    def merge_overlapping_cnvs(self, existStarts: iter, existEnds: iter, existCNs: iter, newStart: int, newEnd: int, newCN:int):
        """
        Handle overlapping copy number segments, merging and editing existing segments as necessary

        In essence, if this segment overlaps an existing segment with the same copy number state, those segments are merged
        together. If the copy number state is different, the largest increase is kept,
         truncating or even breaking apart the copy-neutral segment in the process. In the worse case senario,
        this will lead to three different segments being created

        :param existStarts: The start position of the existing segment
        :param existEnds: The end position of the existing segment
        :param existCNs: The copy number state of the existing segment
        :param newStart: The start position of the new segment
        :param newEnd: The end position of the new segment
        :param newCN: The copy number state of the new segment
        :return: A tuple containing the new and old events. Ex {[newEvent1, newEvent2], [oldEvent1, oldEvent2]}
        """

        def reduce_new_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd):

            # The existing event has higher priority than the old event
            # Truncate the newer event
            # Since this could break the newer event apart into two pieces, lets do that now, and see
            # if the output segments are valid
            outStart1 = newStart
            outEnd1 = oldStart
            outStart2 = oldEnd
            outEnd2 = newEnd
            if outStart1 < outEnd1:  # Valid segment, process
                existStarts, existEnds, existCNs = self.merge_overlapping_cnvs(existStarts, existEnds, existCNs,
                                                                             outStart1, outEnd1, newCN)
            if outStart2 < outEnd2:  # Valid segment
                existStarts, existEnds, existCNs = self.merge_overlapping_cnvs(existStarts, existEnds, existCNs,
                                                                             outStart2, outEnd2, newCN)
            return existStarts, existEnds, existCNs

        def reduce_old_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd, oldCN):

            # The newer event is a "higher"-level deletion
            # Split/Truncate the older event
            outStart1 = oldStart
            outEnd1 = newStart
            outStart2 = newEnd
            outEnd2 = oldEnd
            outCN = oldCN
            # Delete existing event
            existStarts.pop(start_bisect)
            existEnds.pop(start_bisect)
            existCNs.pop(start_bisect)

            if outStart2 < outEnd2:  # Valid segment, do the last one first to maintain sorted order
                existStarts.insert(start_bisect, outStart2)
                existEnds.insert(start_bisect, outEnd2)
                existCNs.insert(start_bisect, outCN)
            if outStart1 < outEnd1:  # Also valid
                existStarts.insert(start_bisect, outStart1)
                existEnds.insert(start_bisect, outEnd1)
                existCNs.insert(start_bisect, outCN)

            # Check for any more overlaps
            return self.merge_overlapping_cnvs(existStarts, existEnds, existCNs, newStart, newEnd, newCN)

        # First, does this new segment actually overlap anything?
        start_bisect = bisect.bisect_right(existEnds, newStart)
        end_bisect = bisect.bisect_left(existStarts, newEnd)

        if start_bisect == end_bisect:
            # There are no overlaps. Simply add this new event in, and return it
            existStarts.insert(start_bisect, newStart)
            existEnds.insert(start_bisect, newEnd)
            existCNs.insert(start_bisect, newCN)
            return existStarts, existEnds, existCNs

        # Grab the first overlap
        oldStart = existStarts[start_bisect]
        oldEnd = existEnds[start_bisect]
        oldCN = existCNs[start_bisect]

        # Simplest case first. If both these events have the same CN state, lets merge them together
        if oldCN == newCN:

            outStart = oldStart if oldStart < newStart else newStart
            outEnd = oldEnd if oldEnd > newEnd else newEnd
            # Delete the existing event
            existStarts.pop(start_bisect)
            existEnds.pop(start_bisect)
            existCNs.pop(start_bisect)

            # Check for any more overlaps
            return self.merge_overlapping_cnvs(existStarts, existEnds, existCNs, outStart, outEnd, newCN)
        else:
            # These segments overlap and have different CN states.
            # Lets keep the highest-level event
            if newCN <= 2 and oldCN <= 2: # Deletion
                if oldCN < newCN:
                    # The older event is a "higher"-level deletion
                    return reduce_new_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd)
                else:
                    return reduce_old_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd, oldCN)
            if newCN >= 2 and oldCN >= 2:  # Gain/Amp
                if oldCN > newCN:
                    # The older event is a higher level gain. Split/truncate the new event
                    return reduce_new_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart,
                                            oldEnd)
                else:
                    return reduce_old_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd,
                                          oldCN)
            else:
                # One event must be a gain/amp, while the other is a deletion. In this case, keep both, and subset the
                # larger event by the smaller one
                if oldEnd - oldStart < newEnd - newStart:
                    # The older event is smaller. Split the newer event
                    return reduce_new_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart,
                                            oldEnd)
                else:
                    # The newer event is smaller. We should split the older event
                    return reduce_old_event(existStarts, existEnds, existCNs, newStart, newEnd, newCN, oldStart, oldEnd,
                                          oldCN)


    def add(self, chrom: str, start: int, end: int, cn: int):

        # For LymphGen compatability: Strip "chr" prefix
        chrom = chrom.replace("chr", "")

        if end <= start:
            if start == end:
                # This segment has a length of 0?!. Skip this I guesss
                return None
            else:
                # This segment has a negative length??
                raise TypeError("Invalid CNV segment: \'%s:%s-%s\'" % (chrom, start, end))

        # Have we seen any events for this chromosome before?
        if chrom not in self.len_seg:
            # If not, just store this segment. Simple!
            self.starts[chrom] = [start]
            self.ends[chrom] = [end]
            self.cn_states[chrom] = [cn]
            self.len_seg[chrom] = 1

            # Are we using chr-prefixed contig names?
            if self.is_chr_prefixed is None:
                self.is_chr_prefixed = True if chrom.startswith("chr") else False
        else:
            # If we have seen events on this chromosome before, we need to compare this new segment with existing segments
            # In theory, events should be non-overlapping, but I don't want to assume that because then things will break
            # horribly later

            self.starts[chrom], self.ends[chrom], self.cn_states[chrom] = \
                self.merge_overlapping_cnvs(self.starts[chrom], self.ends[chrom], self.cn_states[chrom], start, end, cn)
            self.len_seg[chrom] = len(self.cn_states)


    def overlap_chrom(self, chromosome: Chromosome, threshold: float = 0.8):

        """
        Calculates the overlap between the segments stored here and a given chromosome
        :param chromosome: A Chromosome() object defining the start and end of the p and q arms, respectively
        :return: A dictionary containing
        """

        # Do the CNVs within this chromosome encompass a given chromosome more than the specified threshold?
        chrom_name = chromosome.chrom

        # Handle "chr" prefix nonsense
        if self.is_chr_prefixed and not chrom_name.startswith("chr"):
            chrom_name = "chr" + chrom_name
        elif not self.is_chr_prefixed and chrom_name.startswith("chr"):
            chrom_name = chrom_name.replace("chr", "")

        try:
            chrom_starts = self.starts[chrom_name]
        except KeyError:
            # No CN events were provided for this chromosome, hence there can be no overlap
            return {}
        chrom_ends = self.ends[chrom_name]
        chrom_cn = self.cn_states[chrom_name]

        homdel_sum = {"p": 0, "q": 0, "Chrom": 0}
        del_sum = {"p": 0, "q": 0, "Chrom": 0}
        gain_sum = {"p": 0, "q": 0, "Chrom": 0}
        amp_sum = {"p": 0, "q": 0, "Chrom": 0}

        # Check for overlapping segments for each arm
        for (start, end, cn) in zip(chrom_starts, chrom_ends, chrom_cn):

            # Ignore copy-neutral segments
            if cn == 2:
                continue

            if end < chromosome.p_start or start > chromosome.q_end:
                # Segment falls out of range
                continue
            if start < chromosome.p_end:
                olap_start = start if start > chromosome.p_start else chromosome.p_start
                olap_end = end if end < chromosome.p_end else chromosome.p_end
                if cn < 2:
                    del_sum["p"] += olap_end - olap_start
                    del_sum["Chrom"] +=  olap_end - olap_start
                    if cn < 1:
                        homdel_sum["p"] += olap_end - olap_start
                        homdel_sum["Chrom"] += olap_end - olap_start
                elif cn > 2:
                    gain_sum["p"] += olap_end - olap_start
                    gain_sum["Chrom"] += olap_end - olap_start
                    if cn > 3:
                        amp_sum["p"] += olap_end - olap_start
                        amp_sum["Chrom"] += olap_end - olap_start

            if end > chromosome.q_start:  # We use an if, not elif, in case a segment overlaps both the p and q arm
                olap_start = start if start > chromosome.q_start else chromosome.q_start
                olap_end = end if end < chromosome.q_end else chromosome.q_end
                if cn < 2:
                    del_sum["q"] += olap_end - olap_start
                    del_sum["Chrom"] +=  olap_end - olap_start
                    if cn < 1:
                        homdel_sum["q"] += olap_end - olap_start
                        homdel_sum["Chrom"] += olap_end - olap_start
                elif cn > 2:
                    gain_sum["q"] += olap_end - olap_start
                    gain_sum["Chrom"] += olap_end - olap_start
                    if cn > 3:
                        amp_sum["q"] += olap_end - olap_start
                        amp_sum["Chrom"] += olap_end - olap_start

        events = {}
        # Now, calculate the fraction of each overlap
        # Start from the highest level and biggest event, and work our way down/smaller
        # Amplifications/gains
        if amp_sum["Chrom"] / (chromosome.p_length + chromosome.q_length) > threshold: # Whole chromosome Amp
            events[chrom_name + "Chrom"] = "AMP"
        # Now check for arm level events
        else:
            if amp_sum["p"] / chromosome.p_length > threshold:  # p Amp
                events[chrom_name + "p"] = "AMP"
            elif amp_sum["q"] / chromosome.q_length > threshold:  # q Amp
                events[chrom_name + "q"] = "AMP"
            if chrom_name + "Chrom" not in events and gain_sum["Chrom"] / (chromosome.p_length + chromosome.q_length) > threshold:  # Whole chromosome gain
                events[chrom_name + "Chrom"] = "GAIN"
            else:
                if chrom_name + "p" not in events and gain_sum["p"] / chromosome.p_length > threshold:  # p Gain
                    events[chrom_name + "p"] = "GAIN"
                if  chrom_name + "q" not in events and gain_sum["q"] / chromosome.q_length > threshold:  # q Gain
                    events[chrom_name + "q"] = "GAIN"

        # Homozygous and heterozygous deletions
        if homdel_sum["Chrom"] / (chromosome.p_length + chromosome.q_length) > threshold: # Whole chromosome Homozygous deletion
            events[chrom_name + "Chrom"] = "HOMDEL"
        # Now check for arm level events
        else:
            if homdel_sum["p"] / chromosome.p_length > threshold:  # p HOMDEL
                events[chrom_name + "p"] = "HOMDEL"
            elif homdel_sum["q"] / chromosome.q_length > threshold:  # q HOMDEL
                events[chrom_name + "q"] = "HOMDEL"
            if chrom_name + "Chrom" not in events and del_sum["Chrom"] / (chromosome.p_length + chromosome.q_length) > threshold:  # Whole chromosome deletions
                events[chrom_name + "Chrom"] = "HETLOSS"
            else:
                if chrom_name + "p" not in events and del_sum["p"] / chromosome.p_length > threshold:  # p deletion
                    events[chrom_name + "p"] = "HETLOSS"
                if  chrom_name + "q" not in events and del_sum["q"] / chromosome.q_length > threshold:  # q deletions
                    events[chrom_name + "q"] = "HETLOSS"

        return events


    def adjust_ploidy(self, arm_coords, sample_name, redo=False):
        """
        Adjust a sample's CN states based upon ploidy

        Calculate the average copy number state across the entire genome. If a sample is not diploid, normalize all the CN
        changes
        :param arm_coords: A dictionary containing {"chromosome_name": Chromosome()} objects which list chromosomal coordinates
        :param sample_name: A string specifying the sample name. Used for debugging/error purposes
        :param redo: Should we override any existing ploidy state for this sample?
        :return: None
        """

        if self.ploidy is not None and not redo:
            # Ploidy for this sample has already been calculated. Don't do anything
            return None

        # Store the length affected for each ploidy state
        ploidy_cov = {}
        genome_size = 0

        for chromosome in arm_coords.values():
            # Find overlapping CNVs for this arm

            # if the p or q arm coordinates are not set, use a placeholder
            if chromosome.p_start is None:
                chromosome.p_start = -100000
                chromosome.p_end = -100000
                chromosome.p_length = 1
            if chromosome.q_start is None:
                chromosome.q_start = -100000
                chromosome.q_end = -100000
                chromosome.q_length = 1

            # Save the size of this chromosome
            genome_size += chromosome.p_length
            genome_size += chromosome.q_length

            chrom_name = chromosome.chrom
            # Handle "chr" prefix nonsense
            if self.is_chr_prefixed and not chrom_name.startswith("chr"):
                chrom_name = "chr" + chrom_name
            elif not self.is_chr_prefixed and chrom_name.startswith("chr"):
                chrom_name = chrom_name.replace("chr", "")
            try:
                chrom_starts = self.starts[chrom_name]
            except KeyError:
                # No CN events were provided for this chromosome, hence there can be no overlap
                continue
            chrom_ends = self.ends[chrom_name]
            chrom_cn = self.cn_states[chrom_name]

            # Check for overlapping segments for each arm
            for (start, end, cn) in zip(chrom_starts, chrom_ends, chrom_cn):

                # Check p-arm overlap
                if end < chromosome.p_start or start > chromosome.q_end:
                    # Segment falls out of range
                    continue
                if start < chromosome.p_end:
                    olap_start = start if start > chromosome.p_start else chromosome.p_start
                    olap_end = end if end < chromosome.p_end else chromosome.p_end

                    # Store the length of this overlap and associated CN
                    if cn not in ploidy_cov:
                        ploidy_cov[cn] = 0
                    ploidy_cov[cn] += olap_end - olap_start

                if end > chromosome.q_start:  # We use an if, not elif, in case a segment overlaps both the p and q arm
                    olap_start = start if start > chromosome.q_start else chromosome.q_start

                    olap_end = end if end < chromosome.q_end else chromosome.q_end
                    # Store the length of this overlap and associated CN
                    if cn not in ploidy_cov:
                        ploidy_cov[cn] = 0
                    ploidy_cov[cn] += olap_end - olap_start

        # Now that we have calculated the number of bases affected by each CN state, calculate the ploidy
        x = 0
        for cn, bases in ploidy_cov.items():
            x += cn * bases

        ploidy = x / genome_size
        av_ploidy = round(ploidy)

        # Sanity check
        if av_ploidy < 1:
            raise TypeError("Ploidy of sample \'%s\' was calculated to be below 1!" % sample_name)

        # If this tumour is not diploid, adjust the ploidy
        if av_ploidy != 2:
            logging.debug("\'%s\' was calculated to have a ploidy of %s" % (sample_name, av_ploidy))
            ploidy_dif = av_ploidy - 2
            new_cns = {}
            for chrom, cn in self.cn_states.items():
                x = list(y - ploidy_dif for y in cn)
                new_cns[chrom] = x
            self.cn_states = new_cns

        self.ploidy = av_ploidy


def get_args():

    def is_valid_dir(path, parser):
        """
        Checks to ensure the directory path exists
        :param path: A string containing a filepath to a directory
        :param parser: An argparse.ArgumentParser() object
        :return: path, if the string is a valid directory
        :raises: parser.error() if the directory does not exist
        """
        if os.path.exists(path) and os.path.isdir(path):
            return path
        else:
            raise parser.error("Unable to set \'%s\' as the output directory: Not a valid directory" % path)

    def is_valid_file(path, parser):
        """
        Checks to ensure the specified file exists
        :param path: A string containing a filepath
        :param parser: An argparse.ArgumentParser() object
        :return: path, if the string is a valid directory
        :raises: parser.error() if the file does not exist
        """
        if os.path.exists(path) and os.path.isfile(path):
            return path
        else:
            raise parser.error("Unable to locate \'%s\': No such file or directory" % path)

    epilog = os.linesep.join(["The --arms, --entrez_ids, and --lymphgen_genes files can be found in the \'resources\' folder. ",
                              "Note that genome and exome sequencing types are handled exactly the same. ",
                              "The --entrez-ids file must have the Hugo_Symbol under the column \"Approved Symbol\" and the Entrez ID under the column \"NCBI Gene ID(supplied by NCBI)\". ",
                              "The --cnvs file should contain the following colummns: Tumor_Sample_Barcode, chromosome, start, end, CN. ",
                              "The --arms file should contain the following columns: chromosome, start, end, arm. "
                              ])
    parser = argparse.ArgumentParser(description="Generates input files for the LymphGen classifier\nVersion 1.0.0", epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    snvs_args = parser.add_argument_group("Input mutation files")
    snvs_args.add_argument("-m", "--maf", metavar="MAF", required=True, type=lambda x: is_valid_file(x, parser), help="Input MAF file listing somatic mutations")
    snvs_args.add_argument("-e", "--entrez_ids", metavar="TSV", required=True, type=lambda x: is_valid_file(x, parser), help="A tab-delimited file containing gene names (Hugo Symbol) and the corresponding Entrez gene ID")
    cnv_args = parser.add_argument_group("(Optional) Input CNV files")
    cnv_args.add_argument("-c", "--cnvs", metavar="TSV", default=None, type=lambda x: is_valid_file(x, parser), help="Input tab-delimited file summarizing copy number events")
    cnv_args.add_argument("--log2", action="store_true", help="Does the input CNV file use log2 ratios? (i.e. log2(absoluteCN) - 1)")
    cnv_args.add_argument("-g", "--genes", metavar="BED", default=None, type=lambda x: is_valid_file(x, parser), help="Input BED4+ file listing start and end positions of genes/exons")
    cnv_args.add_argument("-a", "--arms", metavar="TSV", default=None, type=lambda x: is_valid_file(x, parser), help="Input tab-delimited file listing the positions of chromosome arms")

    parser.add_argument("-l", "--lymphgen_genes", metavar="TXT", default=None, type=lambda x: is_valid_file(x, parser), required=False, help="An optional file listing all Entrez IDs considered by the LymphGen classifier OR listing Entrez IDs or Hugo_Symbols of genes covered by a targeted capture panel. Output will be subset to these genes")
    parser.add_argument("-o", "--outdir", metavar="PATH", required=True, type=lambda x: is_valid_dir(x, parser), help="Output directory for LymphGen input files")
    parser.add_argument("--outprefix", metavar="STRING", default=None, help="Output files will be prefixed using this string [Default: Use the base name of the MAF file]")
    parser.add_argument("-v", "--verbose", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default="INFO", help="Set logging verbosity")

    args = parser.parse_args()
    # If no outprefix was set, use the basename of the maf file as the output prefix
    if args.outprefix is None:
        args.outprefix = os.path.basename(args.maf).split(".")[0]

    # If --cnvs is specified, the annotation files are also required
    if args.cnvs:
        if not args.genes or not args.arms:
            raise parser.error("--genes and --arms are required if --cnvs is specified")

    logging.basicConfig(level=getattr(logging, args.verbose), format='%(levelname)s:%(message)s')

    return args


def load_entrez_ids(entrez_file: str, hugo_column: str="Approved symbol", entrez_column: str="NCBI Gene ID(supplied by NCBI)",
                    status_column: str="Status", prev_name_column: str="Previous symbols"):
    """
    Creates a dictionary which contains the gene name (Hugo Symbol) and corresponding entrez ID (NCBI ID) from the
    specified text file

    Note the input file must have a header. An example file can be obtained from https://www.genenames.org/download/custom/
    Ensure the "Approved symbol" and "NCBI Gene ID" columns are selected. The default column names reflect those obtained
    from this source.
    The "Approval Status" column is optional, but will be used to remove gene names which have since been withdrawn.
    If the "Previous symbols" solumn is included, these gene names will also be assigned to that Entrez ID
    The subset_file should list one Entrez ID per line
    All extra columns are ignored

    :param entrez_file: A string containing a filepath to a tsv file containing the gene names and gene IDs
    :param hugo_column: A string specifying the name of the column which contains
    :param entrez_column: A string specifying the name of the column which contains the Entrez gene ID
    :param status_column: A string specifying the name of the column which contains the Approval Status. Optional.
    :param prev_name_column: A string specifting the name of the column which specifies previously used names for each gene. Optional
    :return: Two dictionaries containing {"Gene Name": "Entrez_ID"}
    """
    hugo_name_to_id = {}
    alt_hugo_name_to_id = {}  # If Previous symbols are included
    skipped_genes = []

    hugo_col_num = None
    entrez_col_num = None
    status_col_num = None  # Optional
    prev_name_col_num = None  # Optional
    i = 0

    with open(entrez_file) as f:
        for line in f:
            i += 1
            line = line.rstrip("\n").rstrip("\r")  # Remove line endings
            cols = line.split("\t")
            if len(cols) < 2:  # Sanity check this file has multiple columns
                raise TypeError("Input file %s does not appear to be a tab-delimited file" % entrez_file)

            # If we have not assigned column IDs yet, then we have not processed the file header
            # Load the file header
            if hugo_col_num is None or entrez_col_num is None:
                j = 0
                for col in cols:
                    if col == hugo_column:
                        hugo_col_num = j
                    if col == entrez_column:
                        entrez_col_num = j
                    if status_column is not None and col == status_column:
                        status_col_num = j
                    if prev_name_column is not None and col == prev_name_column:
                        prev_name_col_num = j

                    j += 1
                # Sanity check that we have found all the columns we need
                if hugo_col_num is None:
                    raise AttributeError("Unable to locate a column corresponding to \'%s\' in the input file \'%s\'" % (hugo_column, entrez_file))
                elif entrez_col_num is None:
                    raise AttributeError("Unable to locate a column corresponding to \'%s\' in the input file \'%s\'" % (entrez_column, entrez_file))
                # If we have made it this far, then the header was valid
                continue

            # Check the Approval Status column to see if the gene name has been withdrawn
            if status_col_num is not None:
                if cols[status_col_num] != "Approved":
                    continue

            # Parse the gene name and ID from the input file
            try:
                hugo_name = cols[hugo_col_num]
                entrez_id = cols[entrez_col_num]
                # Sanity check: The entrez ID is ALWAYS a number
                if not entrez_id.isdigit():
                    # If there is no entrez ID, skip this gene
                    if entrez_id == "":
                        skipped_genes.append(hugo_name)
                        continue
                    else:
                        raise TypeError("When parsing line %s of \'%s\', the Entrez gene ID \'%s\' does not appear to be a valid Entrez Gene ID" % (i, entrez_file, entrez_id))

            except IndexError as e:
                # If we end up in here, then there were too few columns in the current line to parse the IDs
                raise AttributeError("Unable to parse line %s of \'%s\': Line is truncated?" % (i, entrez_file)) from e

            # Sanity check: Make sure we haven't seen this gene name before
            if hugo_name in hugo_name_to_id:
                raise AttributeError("Gene \'%s\' appears duplicated in \'%s\'" % (hugo_name, entrez_file))
            hugo_name_to_id[hugo_name] = entrez_id

            # If the Previous symbols column was found, annotate these gene names with the same ID
            previous_names = cols[prev_name_col_num].split(",")
            if previous_names[0] == "":
                continue  # There are no previous names for this gene
            previous_names = [x.replace(" ", "") for x in previous_names]  # Remove extra spaces
            for hugo_name in previous_names:
                # If we have seen this gene name before, then don't overwrite it. Just assume that the first instance
                # encountered is correct. This likely isn't the case, but these cases should be so rare that they are
                # not worth worrying about
                if hugo_name in alt_hugo_name_to_id:
                    continue
                alt_hugo_name_to_id[hugo_name] = entrez_id

    # Final sanity check: As Hugo Symbols and Entrez IDs have a 1-1 mapping, make sure there are no duplicate Entrez IDs
    if len(hugo_name_to_id) != len(set(hugo_name_to_id.values())):
        num_ids = Counter(hugo_name_to_id.values())
        # For simplicity, just print out one Entrez ID which is duplicated
        bad_id = num_ids.most_common(1)[0][0]
        bad_count = num_ids.most_common(1)[0][1]
        raise AttributeError("Entrez gene ID \'\%s\' appears %s times in the input file" % (bad_id, bad_count))

    return hugo_name_to_id, alt_hugo_name_to_id


def load_subset_ids(id_file, out_gene_list, gene_ids: dict, alt_gene_ids: dict = None):
    """
    Loads a set of Hugo_Symbols from a specified file and converts it to ENTREZ.ID. One ID/line. 
    OR loads a set of Entrez IDs from a file. One ID/line. 
    
    Writes the output gene list to file. 
    
    An example of the output gene list:
    "ENTREZ.ID"
    60
    355
    567
    596
    604
    605
    613
    639
    673

    :param id_file: A string specifying the path to a file containing gene IDs as Hugo_Symbol or Entrez ID. 
    :param gene_ids: A dictionary containing the mapping between Hugo_Symbol: Entrez_Gene_ID.
    :param out_gene_list: A string specifying the output path to the gene list
    :param alt_gene_ids: An additional dictionary specifying alternate Hugo_Symbol: Entrez_Gene_ID mappings
    :return: A list specifying Entrez IDs to subset the maf to
    """

    subset_ids = []
    header_written = False
    with open(id_file) as f, open(out_gene_list, "w") as o:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
        
            # If the gene ID is a digit, it's probably already an Entrez ID
            if line.isdigit(): 
                entrez_id = line
            else: 
                hugo_name = line
                try:
                    entrez_id = gene_ids[hugo_name]
                except KeyError:
                    # If this gene does not have a Entrez ID, are we prehaps using an older Hugo_Symbol?
                    if hugo_name in alt_gene_ids:
                        entrez_id = alt_gene_ids[hugo_name]
                    else:
                        # Skip this mutation if there is no Entrez ID
                        logging.warning("Hugo_Symbol \'%s\' has no matching Entrez ID" % hugo_name)
                        continue

            subset_ids.append(entrez_id)
            # Write this ID to file
            if not header_written: 
                header_written = True
                o.write("\"ENTREZ.ID\"" + os.linesep)
                
            o.write(entrez_id)
            o.write(os.linesep)
            
            
    subset_ids = set(subset_ids)
    return subset_ids


def generate_mut_flat(in_maf: str, gene_ids: dict, out_mut_flat: str, alt_gene_ids: dict = None, subset_ids: iter = None):
    """
    Converts a MAF file into the mutation flat file and gene list used by LymphGen
    WARNING: Only GRCh37/hg19 is currently supported!

    An example of the output mutation FLAT file:
    Sample	ENTREZ.ID	Type	Location
    Sample1	604	MUTATION	187451395
    Sample1	604	MUTATION	187451408
    Sample1	613	MUTATION	23523596
    Sample1	970	TRUNC	6590925
    Sample1	1840	Synon	113495932

    Subsets the maf file to the list of genes provided with the subset_ids argument. If the default included lymphgen_genes.txt 
    file is used, all genes currently used in LymphGen classification will be included. If a list of hugo_symbols that represents
    a targeted sequencing panel is provided, the maf will be subset to the specified genes. 

    The following MAF columns are required: Hugo_Symbol, Variant_Classification, Tumor_Sample_Barcode. If NCBI_Build and
    Start_Position are found, the output mutation flat file will contain an additional column specifying "Location".
    If Tumor_Seq_Allele2 is provided, the MYD88 hotspot mutations will be annotated as "L265P"

    :param in_maf: A string containing a filepath to a MAF file containing the variants of interest
    :param gene_ids: A dictionary containing the mapping between Hugo_Symbol: Entrez_Gene_ID.
    :param out_mut_flat: A string specifying the output path to the mutation flat file
    :param alt_gene_ids: An additional dictionary specifying alternate Hugo_Symbol: Entrez_Gene_ID mappings
    :param subset_ids: An interable specifying a list of Entrez IDs. Only mutations within these Entrez IDs will be output
    :return: A list specifying all samples analyzed for mutations
    """
    logging.info("Processing MAF file")

    # Non-synonmymous mutation types:
    non_syn = {"In_Frame_Del", "In_Frame_Ins", "Missense_Mutation"}
    trunc_mut = {"Frame_Shift_Del", "Frame_Shift_Ins", "Nonsense_Mutation", "Nonstop_Mutation", "Splice_Site", "Translation_Start_Site"}
    synon_mut = {"Silent", "5'UTR", "5'Flank", "Intron", "3'UTR"}  # Use for the "Synon" mutation type

    # Which samples are we analyzing?
    sample_list = SortedSet()  # Used to ensure the output files are in somewhat sorted order

    required_cols = ["Hugo_Symbol", "Variant_Classification", "Tumor_Sample_Barcode"]
    optional_cols = ["NCBI_Build", "Start_Position", "Tumor_Seq_Allele2"]
    header_cols = {}
    out_header_written = False
    out_header_cols = ["Sample", "ENTREZ.ID", "Type"]
    if alt_gene_ids is None:
        alt_gene_ids = {}

    # 
    genes_seen = {}
    skipped_mut = []  # Store mutations in genes with no Entrez ID

    i = 0
    with open(in_maf) as f, open(out_mut_flat, "w") as o:
        for line in f:
            i += 1
            # Ignore comment lines
            if line[0] == "#":
                continue
            line = line.rstrip("\n").rstrip("\r")
            cols = line.split("\t")

            # Skip empty lines
            if not line:
                continue

            # If we haven't parsed the header yet, assume we are parsing the first line of the file, and that is the header
            if not header_cols:
                j = 0
                for col in cols:
                    if col in required_cols:
                        header_cols[col] = j
                    elif col in optional_cols:
                        header_cols[col] = j
                    j += 1

                # Check that we have all required columns
                for col in required_cols:
                    if col not in header_cols:
                        raise AttributeError("Unable to locate a column specifying \'%s\' in the MAF file header" % col)
                # See which optional columns we have
                if "Start_Position" in header_cols:
                    out_header_cols.append("Location")
                    logging.info("\'Start_Position\' column found in MAF file. Output mutation flat file will be annotated with \'Position\'")
                if "Tumor_Seq_Allele2" in header_cols:
                    logging.info(
                        "\'Tumor_Seq_Allele2\' column found in MAF file. MYD88 hotspot mutations will be annotated as \'L265P\'")
                continue

            # Process mutations
            mut_attributes = {x: cols[y] for x, y in header_cols.items()}

            # Check genome build, as only GRCh37 is supported
            if "NCBI_Build" in mut_attributes and "Start_Position" in mut_attributes and mut_attributes["NCBI_Build"] != "GRCh37":
                raise NotImplementedError("Only GRCh37 is currently supported as a reference genome")

            # Lets get the easiest stuff out of the way first. Specify the output sample name and gene ID
            sample_name = mut_attributes["Tumor_Sample_Barcode"]
            if sample_name == "" or sample_name == ".":
                raise AttributeError("No tumor_sample_barcode was found when processing line %s of the MAF file" % i)

            hugo_name = mut_attributes["Hugo_Symbol"]
            try:
                # Skip intergenic mutations
                if hugo_name == "Unknown":
                    continue
                entrez_id = gene_ids[hugo_name]
            except KeyError:
                # If this gene does not have a Entrez ID, are we prehaps using an older Hugo_Symbol?
                if hugo_name in alt_gene_ids:
                    entrez_id = alt_gene_ids[hugo_name]
                else:
                    # Skip this mutation if there is no Entrez ID
                    skipped_mut.append(line)
                    continue

            # Obtain position (if Start_Position is specified)
            if "Start_Position" in mut_attributes:
                position = mut_attributes["Start_Position"]
            else:
                position = None

            # Finally, specify the variant type
            if mut_attributes["Variant_Classification"] in trunc_mut:
                genes_seen[entrez_id] = hugo_name  # This gene has a non-synonmymous mutation, so lets assume we have sequenced it
                type = "TRUNC"
            elif mut_attributes["Variant_Classification"] in non_syn:
                genes_seen[entrez_id] = hugo_name  # This gene has a non-synonmymous mutation, so lets assume we have sequenced it
                type = "MUTATION"
            elif mut_attributes["Variant_Classification"] in synon_mut:
                genes_seen[entrez_id] = hugo_name
                type = "Synon"
            else:
                # Ignore intronic mutations, 3'UTRs etc
                continue

            # If this mutation is in MYD88, does it affect the hotspot?
            if hugo_name == "MYD88" and "Tumor_Seq_Allele2" in mut_attributes:
                if mut_attributes["Start_Position"] == "38182641" and mut_attributes["Tumor_Seq_Allele2"] == "C":
                    type = "L265P"

            if sample_name not in sample_list:
                sample_list.add(sample_name)

            # If this gene isn't in the subset list provide it, skip it
            if subset_ids is not None and entrez_id not in subset_ids:
                continue

            # Finally, write this variant
            # Write a header line, if we have not done so already
            if not out_header_written:
                out_header_written = True
                o.write("\t".join(out_header_cols))
                o.write(os.linesep)

            out_line = [sample_name, entrez_id, type, position + os.linesep]
            o.write("\t".join(out_line))

    # Check to see that we actually converted mutations
    if len(genes_seen) == 0:
        logging.warning("No mutations were successfully converted. Check that the --entrez_ids file has valid Hugo Symbols matching the --maf file")
    elif skipped_mut:
        logging.warning("%s mutations in the MAF file were not converted. These either don't have a valid Entrez ID, or they were not in the --lymphgen_gene file" % len(skipped_mut))

    logging.info("Finished processing MAF file")
    
    return list(sample_list)


def load_gene_coords_bed(bed_file, gene_ids, alt_gene_ids=None):
    """
    Load in the genomic coordinates of genes in the human genome from the specified BED4+ file.

    The following columns are required (no header)
    chrom   start   end gene

    A gene can be split across multiple BED entries (ex. exonic coordinates). In this case, the start and end of the first and last exon,
    respectively, will be used as the gene start/end coordinates

    :param bed_file: A string containing a filepath to a BED4+ file listing the positions of genes
    :return: A dictionary storing {gene_name: Gene()}
    """
    if alt_gene_ids is None:
        alt_gene_ids = {}
    gene_coords = {}
    skipped_genes = []

    i = 0
    with open(bed_file) as f:
        for line in f:
            i += 1
            line = line.rstrip("\n").rstrip("\r")  # Remove line endings
            cols = line.split("\t")

            # Skip empty lines
            if not line:
                continue

            # Since this BED file could contain more than 4 columns (ex. a BED6 or BED12 file), manually unpack and inspect the first
            # four columns, since those are the columns we care about
            try:
                chrom = cols[0]
                start = cols[1]
                end = cols[2]
                gene = cols[3]
            except IndexError:
                # This BED entry was trucated
                raise AttributeError("Unable to parse line %s of %s as it contains less than four columns (chrom, start, end, gene)" % (i, bed_file))

            # Check that the start and end are actually genomic coordinates
            if not start.isdigit():
                raise TypeError("When parsing line %s of %s, the start position \'%s\' is not a valid genomic coordinate" % (i, bed_file, start))
            if not end.isdigit():
                raise TypeError("When parsing line %s of %s, the end position \'%s\' is not a valid genomic coordinate" % (i, bed_file, end))

            start = int(start)
            end = int(end)
            chrom = chrom.replace("chr", "")

            # Is the gene name the Hugo Symbol or the Entrez gene ID?
            # If its an integer, lets assume its the Entrez ID
            # If its not, assume it is the Hugo Symbol and convert it to the Entrez ID
            if not gene.isdigit():
                try:
                    entrez_id = gene_ids[gene]
                except KeyError:
                    # If we can't find this Hugo Symbol in the default mappings, check the alt gene IDs
                    if gene in alt_gene_ids:
                        entrez_id = alt_gene_ids[gene]
                    else:
                        # This gene doesn't have an Entrez ID. Skip it
                        skipped_genes.append(gene)
                        continue
            else:
                entrez_id = gene

            # Have we seen this chromosome before?
            if chrom not in gene_coords:
                gene_coords[chrom] = {}
            # Have we seen this gene before?
            if entrez_id in gene_coords[chrom]:
                # If so, then we need to update the start/end of this gene based on this new BED entry
                existing_gene_entry = gene_coords[chrom][entrez_id]

                # Sanity check
                if chrom != existing_gene_entry.chrom:
                    raise AttributeError("We found two entries for gene \'%s\'. One is found on chromosome \'%s\', while the other is found on \'%s\'"
                                         % (gene, chrom, existing_gene_entry.chrom))
                if start < existing_gene_entry.start:
                    existing_gene_entry.start = start
                if end > existing_gene_entry.end:
                    existing_gene_entry.end = end
            else:
                # If we haven't seen this gene before, create a new entry for it
                gene_coords[chrom][entrez_id] = Gene(chrom, start, end, entrez_id)

    # Now that we have processed all genes, make sure we actually found Entrez IDs for a handful of genes
    # If we haven't, it means the input file likely didn't contain Hugo Symbols
    if len(gene_coords) == 0:
        raise AttributeError("No Hugo Symbols from the --genes file (column 4) were found in the --entrez_ids file. An example gene is %s" % (skipped_genes[0]))

    return gene_coords


def load_chrom_arm(arm_file):
    """
    Loads in the genomic coordinates corresponding to the arm of each chromosome

    The input should be a tab-delimited file with the following columns
    chromosome  start   end arm

    :param arm_file: A string containing a filepath to a tab-delimited file containing genomic coordinates for chromosome arms
    :return: A dictionary storing the genomic range of each chromosome, and each individual arm
    """

    arm_chrom = {}
    required_cols = ["chromosome", "start", "end", "arm"]
    header_cols = {}

    i = 0
    with open(arm_file) as f:
        for line in f:
            i += 1
            line = line.rstrip("\n").rstrip("\r")  # Remove line endings
            cols = line.split("\t")

            # Skip empty lines
            if not line:
                continue

            # If we haven't parsed the header yet, assume this is the first line of the file (aka the header)
            if not header_cols:
                j = 0
                for col in cols:
                    if col in required_cols:
                        header_cols[col] = j
                    j += 1

                # Check to make sure all required columns are found
                for col in required_cols:
                    if col not in header_cols:
                        raise AttributeError("Unable to locate column %s in the chromosome arm positions file \'%s\'" % (col, arm_file))
                # If we get this far, the header is valid
                continue

            try:
                arm_attributes = {x: cols[y] for x, y in header_cols.items()}
            except IndexError:
                # We were unable to find a required column. This line is likely truncated
                raise AttributeError("Unable to parse line %s of the chromosome arm positions file %s: The line appears truncated" % (i, arm_file))

            # Have we seen this chromosome before? If not, lets create an object to store its genomic features
            chrom = arm_attributes["chromosome"]

            # Strip chr prefix
            chrom = chrom.replace("chr", "")

            if chrom not in arm_chrom:
                arm_chrom[chrom] = Chromosome(chrom)

            # Save the arm coordinates in the chromosome
            try:
                arm_chrom[chrom].add(int(arm_attributes["start"]), int(arm_attributes["end"]), arm_attributes["arm"])
            except ValueError as e:
                raise TypeError("Unable to process line %s of \'%s\': start and end must be integers") from e

    return arm_chrom


def get_overlap_genes(chrom: str, start: int, end: int, copy_num: int, gene_cords: dict):

    """
    Find the genes which overlap a given segment

    This is a very brute-force approach which will check all genes on the target chromosome for overlap. Pre-sorting and bisecting genomic
    regions would be significantly faster, but 1) You need to handle overlapping genes, and 2) I am assuming the performance penalty won't
    matter too much.

    Note that gains and losses are handled differently if a segment partially overlaps a gene. If the event is a loss, the gene is included.
    If the event is a gain, the gene is NOT included, as that copy is likely not functional

    :param chrom: A string coresponding to the contig name
    :param start: An int specifying the start of the segment
    :param end: An int specifying the end of the segment
    :param copy_num: An integer specifying the copy number of this segment
    :param gene_cords: A dictionary storing the positions of genes, in the format {chromosome: {gene1: attr, gene2: attr...}}
    :return:
    """

    # Handle chr-prefix shenanigans
    is_chr_prefixed = next(iter(gene_cords.keys())).startswith("chr")
    if is_chr_prefixed and not chrom.startswith("chr"):
        chrom = "chr" + chrom
    elif not is_chr_prefixed and chrom.startswith("chr"):
        chrom = chrom.replace("chr", "")

    try:
        genes_on_chrom = gene_cords[chrom]
    except KeyError:
        # No genes are on this contig. This could be a user error, or this a contig with no annotated genes
        return []

    # Go through each gene on this chromosome and see if the coordinates overlap with our regions
    olap_genes = []
    for entrez_id, gene_info in genes_on_chrom.items():
        if start < gene_info.end and end > gene_info.start:
            # Overlap found
            # Now for the tricky part
            # If the segment partially overlaps this gene, then we need to handle gains and losses differently
            # Deleting or gaining half of a gene will likely cause it to no longer function
            # Thus, partial deletions of genes could be drivers, while partial gains of genes are likely never drivers
            if copy_num < 2:
                olap_genes.append(entrez_id)
            elif copy_num > 2:
                if start > gene_info.start or end < gene_info.end:
                    continue  # Partially overlapping
                else:
                    olap_genes.append(entrez_id)
            else:
                raise NotImplementedError("Not calculating overlapping genes for copy-neutral segments")

    return olap_genes


def generate_cnv_files(cnv_segs, gene_regions_bed, arm_regions, gene_ids, out_cnv_gene, out_cnv_arm, sample_ids, alt_gene_ids=None, subset_ids=None, input_log2: bool= False, focal_cn_thresh:int = 30000000):
    """
    Characterize focal and arm-level copy number events, and summarize them in the respective output files.

    For focal events (i.e. events smaller than the specified threshold), identify all genes which overlap the even, and save them to the out_cnv_gene file
    All events are used to flag chromosomes or individual chromosomal arms which are gained or amplified.

    The cnv_segs file should have the following columns (extra columns are ignored):
    Tumor_Sample_Barcode    chromosome  start"  end CN

    Where CN is the absolute copy number

    The gene_regions_bed can have multiple entries for each gene (ex. exons). The maximum and minimum of the cumulative entries is used to define the
    gene boundaries

    The arm_regions file should contain the following columns:
    chromosome  start   end arm

    :param cnv_segs: A string containing a filepath to a tab-delimited file containing copy number events
    :param gene_regions_bed: A string specifying a filepath to a BED4+ file containing gene positions
    :param arm_regions: A string specifying a filepath to a tab-delimited file specifying the positions of chromosomal arms
    :param gene_ids: A dictionary mapping {Hugo_Symbol: Entrez_ID}
    :param out_cnv_gene: A string specifying the cnv_flat output file
    :param out_cnv_arm: A string specifying the cnv_arm output file
    :param sample_ids: A list containing samples which have one or more somatic mutations
    :param alt_gene_ids: An additional dictionary mapping {Hugo_Symbol: Entrez_IDs}. Used if previous Hugo_Symbols were assigned to a gene
    :param subset_ids: An iterable listing a set of Entrez IDs. Only CNVs overlapping these genes will be output
    :param input_log2: Does the input file contain log2 ratios instead of absolute CN?
    :param focal_cn_thresh: The maximum size of an event for it to be considered "focal", in bp
    :return: A list of samples which have CNV information
    """
    logging.info("Processing copy-number segments")
    if input_log2:
        logging.debug("Converting from log2 ratios")
    # First things first, lets load in the gene regions file and figure out where each gene is
    gene_coords = load_gene_coords_bed(gene_regions_bed, gene_ids, alt_gene_ids=alt_gene_ids)

    # Load in the chromosome arm regions
    arm_coods = load_chrom_arm(arm_regions)

    # Process copy number segments
    required_cols = ["Tumor_Sample_Barcode", "chromosome", "start", "end", "CN"]
    header_cols = {}

    sample_cnvs = {}

    i = 0
    with open(cnv_segs) as f:
        for line in f:
            i += 1
            line = line.rstrip("\n").rstrip("\r")  # Remove line endings
            cols = line.split("\t")

            # Skip empty lines
            if not line:
                continue

            # If we haven't parsed the header yet, assume this is the first line of the file (aka the header)
            if not header_cols:
                j = 0
                for col in cols:
                    if col in required_cols:
                        header_cols[col] = j
                    j += 1

                # Check to make sure all required columns are found
                for col in required_cols:
                    if col not in header_cols:
                        raise AttributeError("Unable to locate column \'%s\' in the CNV segments file \'%s\'" % (col, cnv_segs))
                # If we get this far, the header is valid
                continue

            # Process this CNV entry
            cnv_attributes = {x: cols[y] for x, y in header_cols.items()}

            # Have we processed CNVs from this sample before?
            if cnv_attributes["Tumor_Sample_Barcode"] not in sample_cnvs:
                sample_cnvs[cnv_attributes["Tumor_Sample_Barcode"]] = SampleCNVs()
            # Sterilize input and ensure it is valid, and store these events
            try:
                start = int(cnv_attributes["start"])
                end = int(cnv_attributes["end"])
                # Remove segments which are of length 0
                if start == end:
                    continue
                # Is the CN state absolute CN or log2 ratios? If log2 ratios, convert to absolute CN
                cn_state = float(cnv_attributes["CN"])
                if input_log2:
                    cn_state = math.pow(2, cn_state + 1)
                    if cn_state < 0:
                        cn_state = 0
                elif cn_state < 0:  # Sanity check that the user didn't accidentally provide log2 ratios
                    raise AttributeError("Unable to process line %s of \'%s\': \'%s\': Negative copy number segment. Did you mean to specify the --log2 flag?" % (i, cnv_segs, line))

                cnv_attributes["CN"] = round(cn_state)

                sample_cnvs[cnv_attributes["Tumor_Sample_Barcode"]].add(
                    cnv_attributes["chromosome"],
                    start,
                    end,
                    cnv_attributes["CN"])
            except ValueError as e:
                raise TypeError("Unable to process line %s of \'%s\': start, end, and CN must be numeric" % (i, cnv_segs)) from e
            except TypeError as e:
                raise AttributeError("Unable to process line %s of \'%s\': \'%s\': End coordinate of segment occurs before start" % (i, cnv_segs, line)) from e

    # Now adjust for ploidy
    logging.info("Adjusting sample ploidy")
    for sample, cnvs in sample_cnvs.items():
        cnvs.adjust_ploidy(arm_coods, sample)

    # Now that we have processed all CNVs, lets see which genes have events, and write those out
    logging.info("Calculating gene overlap with CNVs")
    with open(out_cnv_gene, "w") as o:

        # Write output file header
        out_header = ["Sample", "ENTREZ.ID", "Type"]
        o.write("\t".join(out_header))
        o.write(os.linesep)

        # Process segments
        for sample, cnvs in sample_cnvs.items():
            for chrom in cnvs.cn_states.keys():
                # parse each segment
                for start, end, cn_state in zip(cnvs.starts[chrom], cnvs.ends[chrom], cnvs.cn_states[chrom]):

                    # Ignore copy-neutral events
                    if cn_state == 2:
                        continue
                    # Is this event focal? If so, lets find the genes it overlaps
                    if end - start < focal_cn_thresh:
                        # What type of event is this?
                        if cn_state > 3:
                            event_type = "AMP"
                        elif cn_state > 2:
                            event_type = "GAIN"
                        elif cn_state < 1:
                            event_type = "HOMDEL"
                        elif cn_state < 2:
                            event_type = "HETLOSS"
                        else:
                            raise TypeError("Invalid copy number state \'%s\'" % cn_state)

                        olap_genes = get_overlap_genes(chrom, start, end, cn_state, gene_coords)
                        # If any genes overlap, write out these genes
                        for gene in olap_genes:

                            # If a list of genes to subset to was provided, subset those genes
                            if subset_ids is not None and gene not in subset_ids:
                                continue  # Skip this gene, as it was not in the list provided
                            out_line = [sample,
                                        gene,
                                        event_type
                                        ]
                            o.write("\t".join(out_line))
                            o.write(os.linesep)

    # Now that we have processed all the CNVs, identify which samples have arm-level and whole chromosomal copy number changes
    logging.info("Calculating arm-level CNVs")
    with open(out_cnv_arm, "w") as o:
        # Write output header
        out_header = ["Sample", "Arm", "Type"]
        o.write("\t".join(out_header))
        o.write(os.linesep)

        for sample, cnvs in sample_cnvs.items():
            if sample not in sample_ids:
                logging.warning("Sample \'%s\' was provided in the input CNVs file, but no SNVs were found. Skipping..." % sample)
                continue
            for chrom, chrom_info in arm_coods.items():

                # For now, skip chromosome X and Y as those aren't supported?
                if chrom == "X" or chrom == "chrX" or chrom == "Y" or chrom == "chrY":
                    continue

                events = cnvs.overlap_chrom(chrom_info)
                # If there are large-scale CNVs in this sample, output them to the Arm flat file
                for arm, c_type in events.items():
                    out_line = [
                        sample,
                        arm,
                        c_type
                    ]
                    o.write("\t".join(out_line))
                    o.write(os.linesep)

    return list(sample_cnvs.keys())


def generate_sample_annot(samples: iter, cnv_samples: iter, out_sample_annot: str):

    logging.info("Generating sample annotation file")
    with open(out_sample_annot , "w") as o:
        # Write sample annotation file header
        out_header_cols = ["Sample.ID", "Copy.Number", "BCL2.transloc","BCL6.transloc"]
        o.write("\t".join(out_header_cols))
        o.write(os.linesep)

        for sample in list(set(samples + list(cnv_samples))):
            # Were CNVs provided for this sample?
            if sample in cnv_samples:
                has_cn = "1"
            else:
                has_cn = "0"
            out_line = [sample,
                        has_cn,
                        "NA",
                        "NA"]
            o.write("\t".join(out_line))
            o.write(os.linesep)


def main(args=None):

    # Obtain input
    if args is None:
        args = get_args()

    # First, load in the mapping of Gene names and NCBI/Entrez IDs
    gene_ids, alt_gene_ids = load_entrez_ids(args.entrez_ids)

    # If a --lymphgen_genes file was provided, load those genes
    subset_ids = None
    out_gene_list = args.outdir + os.sep + args.outprefix + "_gene_list.txt"
    if args.lymphgen_genes:
        subset_ids = load_subset_ids(args.lymphgen_genes, out_gene_list, gene_ids, alt_gene_ids)

    # Generate the mutation flat file and gene list using the input MAF file and Entrez IDs
    out_mut_flat = args.outdir + os.sep + args.outprefix + "_mutation_flat.tsv"
    sample_list = generate_mut_flat(args.maf, gene_ids, out_mut_flat, alt_gene_ids = alt_gene_ids, subset_ids=subset_ids)

    # Generate the copy number gene list file and arm flat file
    if args.cnvs:
        out_cnv_gene = args.outdir + os.sep + args.outprefix + "_cnv_flat.tsv"
        out_cnv_arm = args.outdir + os.sep + args.outprefix + "_cnv_arm.tsv"
        cnv_sample_list = generate_cnv_files(args.cnvs, args.genes, args.arms, gene_ids, out_cnv_gene, out_cnv_arm, sample_list, alt_gene_ids=alt_gene_ids, subset_ids=subset_ids, input_log2=args.log2)
    else:
        cnv_sample_list = set()

    # Generate a sample annotation file
    out_sample_annot = args.outdir + os.sep + args.outprefix + "_sample_annotation.tsv"
    generate_sample_annot(sample_list, set(cnv_sample_list), out_sample_annot)


if __name__ == "__main__":
    main()
