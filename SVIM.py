__version__ = '0.3'
__author__ = 'David Heller'

import sys
import argparse
import os
import re
import pickle
import gzip
import logging
import configparser

from time import strftime, localtime

from SVIM_COLLECT import guess_file_type, read_file_list, create_full_file, run_full_alignment, analyze_alignment
from SVIM_CLUSTER import cluster_sv_evidences, write_evidence_clusters_bed, write_evidence_clusters_vcf, plot_histograms
from SVIM_COMBINE import combine_clusters


def parse_arguments():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description="""SVIM (pronounced SWIM) is a structural variant caller for long reads. 
It combines full alignment analysis and split-read mapping to 
distinguish six classes of structural variants. SVIM discriminates between similar 
SV classes such as interspersed duplications and cut&paste insertions and is unique 
in its capability of extracting both the genomic origin and destination of insertions 
and duplications.

SVIM consists of three major steps:
- COLLECT detects signatures for SVs in long read alignments
- CLUSTER merges signatures that come from the same SV
- COMBINE combines clusters from different genomic regions and classifies them into distinct SV types


SVIM-COLLECT performs three steps to detect SVs: 
1) Alignment, 
2) SV detection,
3) Clustering""")
    subparsers = parser.add_subparsers(help='modes', dest='sub')
    parser.add_argument('--version', '-v', action='version', version='%(prog)s {version}'.format(version=__version__))

    parser_fasta = subparsers.add_parser('reads', help='Detect SVs from raw reads. Perform steps 1-3.')
    parser_fasta.add_argument('working_dir', type=str, help='working directory')
    parser_fasta.add_argument('reads', type=str, help='Read file (FASTA, FASTQ, gzipped FASTA and FASTQ)')
    parser_fasta.add_argument('genome', type=str, help='Reference genome file (FASTA)')
    parser_fasta.add_argument('--config', type=str, default="{0}/default_config.cfg".format(os.path.dirname(os.path.realpath(__file__))), help='configuration file, default: {0}/default_config.cfg'.format(os.path.dirname(os.path.realpath(__file__))))
    parser_fasta.add_argument('--skip_indel', action='store_true', help='disable indel part')
    parser_fasta.add_argument('--skip_segment', action='store_true', help='disable segment part')
    parser_fasta.add_argument('--cores', type=int, default=1, help='CPU cores to use for alignment')

    parser_bam = subparsers.add_parser('alignment', help='Detect SVs from an existing alignment. Perform steps 2-3.')
    parser_bam.add_argument('working_dir', type=os.path.abspath, help='working directory')
    parser_bam.add_argument('bam_file', type=argparse.FileType('r'), help='SAM/BAM file with aligned long reads (must be query-sorted)')
    parser_bam.add_argument('--config', type=str, default="{0}/default_config.cfg".format(os.path.dirname(os.path.realpath(__file__))), help='configuration file, default: {0}/default_config.cfg'.format(os.path.dirname(os.path.realpath(__file__))))
    parser_bam.add_argument('--skip_indel', action='store_true', help='disable indel part')
    parser_bam.add_argument('--skip_segment', action='store_true', help='disable segment part')

    return parser.parse_args()


def read_parameters(options):
    config = configparser.RawConfigParser(inline_comment_prefixes=';')
    config.read(options.config)

    parameters = dict()
    parameters["min_mapq"] = config.getint("detection", "min_mapq")
    parameters["max_sv_size"] = config.getint("detection", "max_sv_size")
    parameters["min_sv_size"] = config.getint("detection", "min_sv_size")

    parameters["segment_gap_tolerance"] = config.getint("split read", "segment_gap_tolerance")
    parameters["segment_overlap_tolerance"] = config.getint("split read", "segment_overlap_tolerance")

    parameters["distance_metric"] = config.get("clustering", "distance_metric")
    parameters["distance_normalizer"] = config.getint("clustering", "distance_normalizer")
    parameters["partition_max_distance"] = config.getint("clustering", "partition_max_distance")
    parameters["cluster_max_distance"] = config.getfloat("clustering", "cluster_max_distance")

    parameters["del_ins_dup_max_distance"] = config.getfloat("merging", "del_ins_dup_max_distance")
    parameters["trans_destination_partition_max_distance"] = config.getint("merging", "trans_destination_partition_max_distance")
    parameters["trans_partition_max_distance"] = config.getint("merging", "trans_partition_max_distance")
    parameters["trans_sv_max_distance"] = config.getint("merging", "trans_sv_max_distance")

    try:
        parameters["skip_indel"] =  options.skip_indel
    except AttributeError:
        parameters["skip_indel"] =  False
    try:
        parameters["skip_segment"] =  options.skip_segment
    except AttributeError:
        parameters["skip_segment"] =  False

    return parameters


def main():
    # Fetch command-line options and configuration file values and set parameters accordingly
    options = parse_arguments()

    if not options.sub:
        print("Please choose one of the two modes ('reads' or 'alignment'). See --help for more information.")
        return

    parameters = read_parameters(options)

    # Set up logging
    logFormatter = logging.Formatter("%(asctime)s [%(levelname)-7.7s]  %(message)s")
    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.INFO)

    # Create working dir if it does not exist
    if not os.path.exists(options.working_dir):
        os.makedirs(options.working_dir)

    # Create log file
    fileHandler = logging.FileHandler("{0}/SVIM_{1}.log".format(options.working_dir, strftime("%y%m%d_%H%M%S", localtime())), mode="w")
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    logging.info("****************** Start SVIM, version {0} ******************".format(__version__))
    logging.info("CMD: python3 {0}".format(" ".join(sys.argv)))
    logging.info("WORKING DIR: {0}".format(os.path.abspath(options.working_dir)))
    logging.info("****************** STEP 1: COLLECT ******************")

    # Search for SV evidences
    if options.sub == 'reads':
        logging.info("MODE: reads")
        logging.info("INPUT: {0}".format(os.path.abspath(options.reads)))
        logging.info("GENOME: {0}".format(os.path.abspath(options.genome)))
        reads_type = guess_file_type(options.reads)
        if reads_type == "unknown":
            return
        elif reads_type == "list":
            # List of read files
            sv_evidences = []
            for file_path in read_file_list(options.reads):
                reads_type = guess_file_type(file_path)
                full_reads_path = create_full_file(options.working_dir, file_path, reads_type)
                run_full_alignment(options.working_dir, options.genome, full_reads_path, options.cores)
                reads_file_prefix = os.path.splitext(os.path.basename(full_reads_path))[0]
                full_aln = "{0}/{1}_aln.querysorted.bam".format(options.working_dir, reads_file_prefix)
                sv_evidences.extend(analyze_alignment(full_aln, parameters))
        else:
            # Single read file
            full_reads_path = create_full_file(options.working_dir, options.reads, reads_type)
            run_full_alignment(options.working_dir, options.genome, full_reads_path, options.cores)
            reads_file_prefix = os.path.splitext(os.path.basename(full_reads_path))[0]
            full_aln = "{0}/{1}_aln.querysorted.bam".format(options.working_dir, reads_file_prefix)
            sv_evidences = analyze_alignment(full_aln, parameters)
    elif options.sub == 'alignment':
        logging.info("MODE: alignment")
        logging.info("INPUT: {0}".format(os.path.abspath(options.bam_file.name)))
        sv_evidences = analyze_alignment(options.bam_file.name, parameters)

    deletion_evidences = [ev for ev in sv_evidences if ev.type == 'del']
    insertion_evidences = [ev for ev in sv_evidences if ev.type == 'ins']
    inversion_evidences = [ev for ev in sv_evidences if ev.type == 'inv']
    tandem_duplication_evidences = [ev for ev in sv_evidences if ev.type == 'dup']
    translocation_evidences = [ev for ev in sv_evidences if ev.type == 'tra']
    insertion_from_evidences = [ev for ev in sv_evidences if ev.type == 'ins_dup']

    logging.info("Found {0} signatures for deleted regions.".format(len(deletion_evidences)))
    logging.info("Found {0} signatures for inserted regions.".format(len(insertion_evidences)))
    logging.info("Found {0} signatures for inverted regions.".format(len(inversion_evidences)))
    logging.info("Found {0} signatures for tandem duplicated regions.".format(len(tandem_duplication_evidences)))
    logging.info("Found {0} signatures for translocation breakpoints.".format(len(translocation_evidences)))
    logging.info("Found {0} signatures for inserted regions with detected region of origin.".format(len(insertion_from_evidences)))
    
    # Cluster SV evidences
    logging.info("****************** STEP 2: CLUSTER ******************")
    evidence_clusters = cluster_sv_evidences(sv_evidences, parameters)

    # Write SV evidence clusters
    logging.info("Finished clustering. Writing signature clusters..")
    write_evidence_clusters_bed(options.working_dir, evidence_clusters)
    write_evidence_clusters_vcf(options.working_dir, evidence_clusters, __version__)

    # Create result plots
    plot_histograms(options.working_dir, evidence_clusters)

    # Dump obj file
    # evidences_file = open(options.working_dir + '/sv_evidences.obj', 'wb')
    # logging.info("Storing collected evidence clusters into sv_evidences.obj..")
    # pickle.dump(evidence_clusters, evidences_file)
    # evidences_file.close()

    logging.info("****************** STEP 3: COMBINE ******************")
    combine_clusters(evidence_clusters, options.working_dir, parameters, __version__)    

if __name__ == "__main__":
    sys.exit(main())