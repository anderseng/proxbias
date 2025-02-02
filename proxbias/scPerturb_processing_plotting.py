import itertools
import os
from ast import literal_eval
from re import findall
from typing import List, Optional

import infercnvpy
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy
import seaborn as sns
import wget
from scanpy import AnnData
from scipy.stats import zscore
from skimage.measure import block_reduce
from tqdm.auto import tqdm

from proxbias import utils


def _compute_chromosomal_loss(
    anndat: AnnData,
    blocksize: int,
    neigh: int = 150,
    frac_cutoff: float = 0.7,
    cnv_cutoff: float = -0.05,
) -> pd.DataFrame:
    """
    Compute chromosomal loss in both the 3' and 5' regions of the cut site for all gene perturbations in the provided
    AnnData object. The passed AnnData object is expected to have CNV values available as generated by the infercnvpy
    library. Identities of the cells with loss are also recorded in the result DataFrame.
    Note that we calculate the loss for all pairs of perturbations in order to compute the specificity of the loss later
    in the function `generate_specific_loss_and_summary_tables()`. Our objective is to identify the cut sites that
    specifically exhibit loss when that particular site is cut, rather than including the unstable sites that are lost
    when many other sites are cut as well.

    Args:
        anndat (AnnData): AnnData object containing the data with the CNV values.
        blocksize (int): Block size that was used for computing the CNV values by the infercnv call. This is needed for
            identifying the number of blocks to consider for the gene neighborhood specified in the `neigh` parameter.
        neigh (int): Number of neighbor genes to consider. Default is 150 (including the perturbed gene). If there are
            < 150 neighbor genes on the same chromosome as the perturbed gene, this number is capped accordingly.
        frac_cutoff (float): Cutoff fraction for low CNV. Default is 0.7, which means we expect 70% or more of the genes
            in the neighborhood to have low CNV.
        cnv_cutoff (float): CNV cutoff value for "low CNV". Default is -0.05.

    Returns:
        pd.DataFrame: DataFrame containing the computed loss values.

    """
    avar = anndat.var
    cnvarr = anndat.obsm["X_cnv"].toarray() <= cnv_cutoff
    pert_genes = list(set(anndat.obs.gene).intersection(avar.index))
    pert_gene_chr_arm = {
        x: y for x, y in {x: tuple(avar.loc[x][["chromosome", "arm"]]) for x in pert_genes}.items() if not pd.isna(y[0])
    }
    pert_genes_w_chr_info = pert_gene_chr_arm.keys()
    list_aff, list_ko = zip(*itertools.product(pert_genes_w_chr_info, pert_genes))

    loss = pd.DataFrame({"ko_gene": list_ko, "aff_gene": list_aff})
    loss["ko_chr"] = loss.ko_gene.apply(lambda x: pert_gene_chr_arm[x][0] if x in pert_gene_chr_arm else None)
    loss["ko_arm"] = loss.ko_gene.apply(lambda x: pert_gene_chr_arm[x][1] if x in pert_gene_chr_arm else None)
    loss["aff_chr"] = loss.aff_gene.apply(lambda x: pert_gene_chr_arm[x][0])
    loss["aff_arm"] = loss.aff_gene.apply(lambda x: pert_gene_chr_arm[x][1])

    sorted_genes_on_chr = {}
    start_block_of_chr = {}
    end_block_of_chr = {}
    for c in set(map(lambda chr_arm: chr_arm[0], pert_gene_chr_arm.values())):
        sorted_genes_on_chr[c] = list(avar[avar.chromosome == c].sort_values("start").index)
        start_block_of_chr[c] = anndat.uns["cnv"]["chr_pos"][c]
        end_block_of_chr[c] = start_block_of_chr[c] + len(sorted_genes_on_chr[c]) // blocksize

    ko_gene_ixs_dict = {ko_gene: anndat.obs.gene == ko_gene for ko_gene in pert_genes}
    ko_gene_sum_dict = {ko_gene: sum(ixs) for ko_gene, ixs in ko_gene_ixs_dict.items()}

    loss_5p_cells: List[List[str]] = [[]] * len(loss)
    loss_5p_ko_cell_count = np.empty(len(loss))
    loss_5p_ko_cell_frac = np.empty(len(loss))
    loss_3p_cells: List[List[str]] = [[]] * len(loss)
    loss_3p_ko_cell_count = np.empty(len(loss))
    loss_3p_ko_cell_frac = np.empty(len(loss))
    i5p = 0
    i3p = 0

    for aff_gene in tqdm(pert_genes_w_chr_info):
        aff_chr = pert_gene_chr_arm[aff_gene][0]
        # get all genes in the same chromosome with the KO gene, sorted
        sorted_genes = sorted_genes_on_chr[aff_chr]
        # get position of the gene in the chromosome
        aff_gene_ordpos = sorted_genes.index(aff_gene)
        # get block position of the gene in the chromosome
        aff_gene_blocknum_ = aff_gene_ordpos // blocksize
        # get start block of the chromosome gene is in
        aff_chr_startblocknum = start_block_of_chr[aff_chr]
        # get end block of the chromosome gene is in
        aff_chr_endblocknum = end_block_of_chr[aff_chr]
        # get block position of the gene in the genome
        aff_gene_blocknum = aff_chr_startblocknum + aff_gene_blocknum_

        block_count_5p = min(int(neigh / blocksize) - 1, aff_gene_blocknum - aff_chr_startblocknum)
        block_count_3p = min(int(neigh / blocksize) - 1, aff_chr_endblocknum - aff_gene_blocknum)

        blocks_5p = np.arange(aff_gene_blocknum - block_count_5p, aff_gene_blocknum + 1)
        blocks_3p = np.arange(aff_gene_blocknum, aff_gene_blocknum + block_count_3p + 1)

        for t, blocks in {"5p": blocks_5p, "3p": blocks_3p}.items():
            low_frac = np.sum(cnvarr[:, blocks], axis=1) / len(blocks)
            for ko_gene in pert_genes:
                ko_gene_ixs = ko_gene_ixs_dict[ko_gene]
                cells = list(anndat.obs.index[(low_frac >= frac_cutoff) & ko_gene_ixs])
                if t == "5p":
                    loss_5p_cells[i5p] = cells
                    loss_5p_ko_cell_count[i5p] = len(cells)
                    loss_5p_ko_cell_frac[i5p] = len(cells) / ko_gene_sum_dict[ko_gene]
                    i5p += 1
                elif t == "3p":
                    loss_3p_cells[i3p] = cells
                    loss_3p_ko_cell_count[i3p] = len(cells)
                    loss_3p_ko_cell_frac[i3p] = len(cells) / ko_gene_sum_dict[ko_gene]
                    i3p += 1

    loss["loss5p_cells"] = loss_5p_cells
    loss["loss3p_cells"] = loss_3p_cells
    loss["loss5p_cellcount"] = loss_5p_ko_cell_count
    loss["loss3p_cellcount"] = loss_3p_ko_cell_count
    loss["loss5p_cellfrac"] = loss_5p_ko_cell_frac
    loss["loss3p_cellfrac"] = loss_3p_ko_cell_frac

    return loss


def _get_chromosome_info() -> pd.DataFrame:
    """
    Retrieve chromosome information for genes and return it as a DataFrame.

    Returns:
        pd.DataFrame: DataFrame containing chromosome information for genes,
            with gene names as the index and "chromosome" as the column name.

    """
    gene_dict, _, _ = utils.chromosome_info.get_chromosome_info_as_dicts()
    return pd.DataFrame.from_dict(gene_dict, orient="index").rename(columns={"chrom": "chromosome"})


def apply_infercnv_and_save_loss_info(filename: str, blocksize: int = 5, window: int = 100, neigh: int = 150) -> None:
    """
    Apply infercnv and compute loss info on the given data file, and save the results.
    The function loads and processes the data file using the _load_and_process_data() function, applies
    infercnv analysis with the specified parameters, computes loss, and saves the results to a CSV file.
    The result file is saved in the directory specified by utils.constants.DATA_DIR with a name
    generated using the _get_infercnv_result_file() function, which incorporates the `filename`, `blocksize`,
    `window`, and `neigh` values. If the result file already exists, the function skips the infercnv and loss
    computation steps since this is a computationally expensive process.

    Args:
        filename (str): Name of the file to process.
        blocksize (int, optional): Block size for infercnv analysis. Default is 5.
        window (int, optional): Window size for infercnv analysis. Default is 100.
        neigh (int, optional): Number of neighboring genes to consider in loss computation. Default is 150.

    Returns:
        None
    """
    res_path = _get_infercnv_result_file_path(filename, blocksize, window, neigh)
    if not os.path.exists(res_path):
        anndat = _load_and_process_data(filename)
        infercnvpy.tl.infercnv(
            anndat,
            reference_key="perturbation_label",
            reference_cat="control",
            window_size=window,
            step=blocksize,
            exclude_chromosomes=None,
        )
        _compute_chromosomal_loss(anndat, blocksize, neigh).to_csv(res_path, index=False)


def _load_and_process_data(filename: str, chromosome_info: Optional[pd.DataFrame] = None) -> AnnData:
    """
    Load and process the specified file prior to applying `infercnv()`
    The result of the processing is an AnnData object with a 'perturbation_label'
    key specifying the reference category for infercnv analysis.

    Args:
        filename (str): Name of the file to load. Available options: "FrangiehIzar2021_RNA",
            "PapalexiSatija2021_eccite_RNA", "ReplogleWeissman2022_rpe1", "TianKampmann2021_CRISPRi",
            "AdamsonWeissman2016_GSM2406681_10X010".
        chromosome_info (pd.DataFrame, optional): DataFrame containing gene chromosome information.
            Default is None, in which case we use get_chromosome_info() to pull the requried information.

    Returns:
        AnnData: Processed data.
    """
    if chromosome_info is None:
        chromosome_info = _get_chromosome_info()
    print(filename)
    destination_path = os.path.join(str(utils.constants.DATA_DIR), f"{filename}.h5ad")
    if not os.path.exists(destination_path):
        source_path = f"https://zenodo.org/record/7416068/files/{filename}.h5ad?download=1"
        wget.download(source_path, destination_path)
    ad = read_and_log_transform_h5ad_file(destination_path)
    ad.var = ad.var.rename(columns={"start": "st", "end": "en"}).join(chromosome_info, how="left")
    if filename.startswith("Adamson"):
        ad.obs["gene"] = ad.obs.perturbation.apply(lambda x: x.split("_")[0]).fillna("")
    elif filename.startswith("Papalexi"):
        ad.obs["gene"] = ad.obs.perturbation.apply(lambda x: x.split("g")[0] if x != "control" else "").fillna("")
    elif filename.startswith("Replogle"):
        ad.obs["gene"] = ad.obs["gene"].apply(lambda x: x if x != "non-targeting" else "")
    elif filename.startswith(("Frangieh", "Tian")):
        ad.obs["gene"] = ad.obs.perturbation.apply(lambda x: x if x != "control" else "").fillna("")

    ad.obs["perturbation_label"] = ad.obs["gene"].astype("str")
    if filename.startswith("Adamson"):
        ad.obs.loc[pd.isna(ad.obs.perturbation), "perturbation_label"] = "control"
    elif filename.startswith(("Papalexi", "Replogle", "Frangieh", "Tian")):
        ad.obs.loc[ad.obs.perturbation == "control", "perturbation_label"] = "control"

    return ad[ad.obs.perturbation_label != ""]


def _get_telo_centro(arm: str, direction: str) -> Optional[str]:
    """
    Determines the location of a genomic arm within a chromosome based on the chromosome number and direction.

    Args:
        arm (str): The genomic arm represented by the chromosome number followed by 'p' or 'q' (e.g., '1p', '2q', 'Xp').
        direction (str): The direction of the genomic arm (e.g., '5prime', '3prime').

    Returns:
        str: The location of the genomic arm within the chromosome, which can be either 'centromere' or 'telomere'.

    Raises:
        None

    Examples:
        >>> _get_telo_centro('1p', '3prime')
        'centromere'
        >>> _get_telo_centro('2q', '5prime')
        'centromere'
        >>> _get_telo_centro('1p', '5prime')
        'telomere'
        >>> _get_telo_centro('Xq', '3prime')
        'telomere'
    """
    if arm[-1] == "p":
        return "centromere" if "3" in direction else "telomere"
    if arm[-1] == "q":
        return "telomere" if "3" in direction else "centromere"
    return None


def _get_infercnv_result_file_path(filename: str, blocksize: int, window: int, neigh: int) -> str:
    """
    Constructs the file path for the infercnv result file using the given filename, blocksize, window,
    and neigh values. The resulting filename follows the format "{filename}_b{blocksize}_w{window}_n{neigh}.csv"
    under DATA_DIR.

    Args:
        filename (str): The base filename.
        blocksize (int): The blocksize value used by infercnv.
        window (int): The window size value used by infercnv.
        neigh (int): The neighbor gene count value used for loss computation.

    Returns:
        str: The generated infercnv result file path.
    """
    return os.path.join(str(utils.constants.DATA_DIR), f"{filename}_b{blocksize}_w{window}_n{neigh}.csv")


def get_specific_loss_file_path() -> str:
    """
    Returns the file path for the specific loss information file.

    Returns:
        str: The file path for the specific loss information file.
    """
    return os.path.join(str(utils.constants.DATA_DIR), "allres.csv")


def get_specific_loss_summary_file_path() -> str:
    """
    Returns the file path for the specific loss summary table file.

    Returns:
        str: The file path for the specific loss summary table file.
    """
    return os.path.join(str(utils.constants.DATA_DIR), "summaryres.csv")


def _get_short_filename(filename: str) -> str:
    """
    Retrieves the short filename from a given filename by extracting the sequence starting with a capital letter
    until the next capital letter is encountered.

    Args:
        filename (str): The original filename from which the short filename will be extracted.

    Returns:
        str: The short filename containing the first sequence of uppercase letters found in the given filename,
             or an empty string if no uppercase letters are present.

    Raises:
        IndexError: If no uppercase letters are found in the given filename.

    Examples:
        >>> _get_short_filename("PapalexiSatija2021_eccite_RNA")
        'Papalexi'
        >>> _get_short_filename("TianKampmann2021_CRISPRi")
        'Tian'
        >>> _get_short_filename("data_file.csv")
        Raises IndexError since there are no capital letters
    """
    return findall("[A-Z][^A-Z]*", filename)[0]


def generate_specific_loss_and_summary_tables(
    filenames: List[str], blocksize: int = 5, window: int = 100, neigh: int = 150, zscore_cutoff: float = 3.0
) -> None:
    """
    Generates and saves summary chromosomal loss results based on a list of scPerturb AnnData files.
    Filename options to include in `filenames` list are: "FrangiehIzar2021_RNA",
    "PapalexiSatija2021_eccite_RNA", "ReplogleWeissman2022_rpe1", "TianKampmann2021_CRISPRi",
    "AdamsonWeissman2016_GSM2406681_10X010".

    The function performs infercnv on the data files, identifies a list of genes that are exhibiting loss
    specifically around the perturbation site, and aggregates and summarizes the loss information as presented
    in the paper. The results are saved in CSV format.

    Args:
        filenames (List[str]): A list of filenames to process and generate summary results for.
        blocksize (int, optional): Block size for infercnv analysis. Defaults to 5.
        window (int, optional): Window size for infercnv analysis. Defaults to 100.
        neigh (int, optional): Neighbor parameter for loss computation. Defaults to 150.
        zscore_cutoff (float, optional): The loss z-score cutoff value for filtering specific loss. Defaults to 3.0.

    Returns:
        None

    Example usage:
        >>> filenames = ["PapalexiSatija2021_eccite_RNA", "TianKampmann2021_CRISPRi"]
        >>> generate_specific_loss_and_summary_tables(filenames)
    """

    for filename in filenames:
        apply_infercnv_and_save_loss_info(filename, blocksize, window, neigh)
    spec_genes_dict = {}
    for filename in filenames:
        res = pd.read_csv(_get_infercnv_result_file_path(filename, blocksize, window, neigh))
        for c in ["3p", "5p"]:
            col = f"loss{c}_cellfrac"
            col2 = f"loss{c}_cellcount"
            res_c = res[["aff_gene", "ko_gene", col, col2]]
            res_trans = res_c.copy()
            res_trans[col] = res_trans.groupby("aff_gene")[col].transform(lambda x: zscore(x))
            res_trans = (
                res_trans[(res_trans.aff_gene == res_trans.ko_gene) & (res_trans[col2] >= 1)]
                .sort_values(by=col)
                .reset_index(drop=True)
            )
            res_trans = res_trans[res_trans[col] >= zscore_cutoff]
            spec_genes = list(res_trans.ko_gene)
            spec_genes_dict[(filename, c)] = spec_genes

    tested_gene_count_dict = {}
    allres = []
    for filename in filenames:
        res = pd.read_csv(_get_infercnv_result_file_path(filename, blocksize, window, neigh))
        filename_short = _get_short_filename(filename)
        tested_gene_count_dict[filename_short] = len(res.aff_gene.unique())
        for c in ["3p", "5p"]:
            col = f"loss{c}_cellfrac"
            col2 = f"loss{c}_cellcount"
            res_c = res[["aff_gene", "aff_arm", "ko_gene", "ko_arm", col, col2]]
            specific_loss = res_c[
                (res_c.aff_gene == res_c.ko_gene) & res_c.aff_gene.isin(spec_genes_dict[(filename, c)])
            ]
            tmp = specific_loss[["ko_gene", "ko_arm", col, col2]].rename(
                columns={
                    col: "% affected cells",
                    col2: "# affected cells",
                    "ko_gene": "Perturbed gene",
                    "ko_arm": "Chr arm",
                }
            )
            tmp["% affected cells"] = tmp["% affected cells"].apply(lambda x: round(x * 100, 2))
            tmp["Dataset"] = filename_short
            tmp["Perturbation type"] = _get_perturbation_type(filename_short)
            tmp["Tested loss direction"] = c.replace("p", "'")
            allres.append(tmp.sort_values("% affected cells", ascending=False))

    allres_df = pd.concat(allres)
    allres_df["Total # cells"] = allres_df.apply(
        lambda x: int(x["# affected cells"] / x["% affected cells"] * 100), axis=1
    )
    allres_df["Towards telomere or centromere"] = allres_df.apply(
        lambda x: _get_telo_centro(x["Chr arm"], x["Tested loss direction"]), axis=1
    )
    allres_df = allres_df[
        [
            "Perturbed gene",
            "Perturbation type",
            "Dataset",
            "Chr arm",
            "Tested loss direction",
            "Total # cells",
            "# affected cells",
            "% affected cells",
            "Towards telomere or centromere",
        ]
    ]
    allres_df.to_csv(get_specific_loss_file_path(), index=False)

    gr_cols = ["Perturbation type", "Dataset", "Tested loss direction"]
    summaryres_df = (
        allres_df.groupby(gr_cols)
        .agg(
            {"Towards telomere or centromere": [len, lambda x: sum(x == "telomere"), lambda x: sum(x == "centromere")]}
        )
        .reset_index()
    )
    add_cols = [
        "# targets w/ specific loss",
        "# targets w/ loss towards telomere",
        "# targets w/ loss towards centromere",
    ]
    summaryres_df.columns = gr_cols + add_cols  # type: ignore
    summaryres_df["Total # tested targets"] = summaryres_df["Dataset"].apply(lambda x: tested_gene_count_dict[x])
    summaryres_df["% targets w/ specific loss"] = summaryres_df.apply(
        lambda r: round((r["# targets w/ specific loss"] / r["Total # tested targets"]) * 100, 1), axis=1
    )
    cols_order = [
        "Perturbation type",
        "Dataset",
        "Total # tested targets",
        "Tested loss direction",
        "% targets w/ specific loss",
    ] + add_cols
    summaryres_df[cols_order].to_csv(get_specific_loss_summary_file_path(), index=False)


def _get_mid_ticks(lst: List[int]) -> List[float]:
    """
    Takes a list of values and calculates the middle ticks by averaging each value with its preceding
    value and returns a list of the calculated middle ticks. Assumes that `lst` includes 0 as the first item.

    Args:
        lst (List[int]): A list of values.

    Returns:
        List[int]: A list of calculated middle ticks.

    Examples:
        >>> ticks = _get_mid_ticks([0, 10, 20, 30, 40])
        >>> print(ticks)

    """
    return [(lst[i] - lst[i - 1]) / 2 + lst[i - 1] for i in range(1, len(lst))]


def _get_cell_count_threshold(filename_short: str) -> int:
    """
    Retrieves the cell count threshold for displaying the perturbed gene in the heatmap based on the given filename.

    The function returns the cell count threshold based on the provided short filename. The threshold values are defined
    in a dictionary where the short filename serves as the key and the corresponding threshold as the value.

    Args:
        filename_short (str): The short filename to retrieve the cell count threshold for.

    Returns:
        int: The cell count threshold.

    Raises:
        KeyError: If the provided short filename is not found in the predefined mapping.

    Examples:
        >>> threshold = _get_cell_count_threshold("Frangieh")
        >>> print(threshold)
        20
    """
    return {"Frangieh": 20, "Papalexi": 10}[filename_short]


def _get_crunch_size(filename_short: str) -> int:
    """
    Returns the crunch size for a given filename.

    The crunch size is a parameter used to adjust the file size of the generated plot.
    The value depends on the filename and is retrieved from a predefined mapping.

    Args:
        filename_short (str): The short filename for which to retrieve the crunch size.

    Returns:
        int: The crunch size for the given filename.

    Raises:
        KeyError: If the filename_short is not found in the predefined mapping.

    Example:
        >>> crunch_size = _get_crunch_size("Frangieh")
        >>> print(crunch_size)
        30
    """
    return {"Frangieh": 30, "Papalexi": 10}[filename_short]


def _get_perturbation_type(filename_short: str):
    """
    Returns the type of perturbation based on the given shortened filename.

    Args:
        filename_short (str): The short filename for which to retrieve the perturbation type.

    Returns:
        str: The type of perturbation corresponding to the given filename.

    Raises:
        KeyError: If the given filename_short is not found in the predefined mapping.

    Example:
        >>> _get_perturbation_type("Frangieh")
        'CRISPR-cas9'
    """
    return {
        "Frangieh": "CRISPR-cas9",
        "Papalexi": "CRISPR-cas9",
        "Tian": "CRISPRi",
        "Adamson": "CRISPRi",
        "Replogle": "CRISPRi",
    }[filename_short]


def read_and_log_transform_h5ad_file(filename: str) -> AnnData:
    """
    Read and log-transform the specified h5ad single-cell perturb-seq file.

    Args:
        filename (str): The name of the dataset file to read and log-transform.

    Returns:
        AnnData: The log-transformed dataset as an AnnData object.
    """
    ad = scanpy.read_h5ad(filename)
    scanpy.pp.log1p(ad)
    return ad


def plot_loss_for_selected_genes(
    filenames: List[str],
    chromosome_info: Optional[pd.DataFrame] = None,
    blocksize: int = 5,
    window: int = 100,
    neigh: int = 150,
) -> None:
    """
    Plot the specific losses using infercnv analysis for the given list of filenames.
    The function loads the "allres.csv" file, reads it into a DataFrame, and sets the necessary plotting configurations.
    If `chromosome_info` is not provided, it calls `_get_chromosome_info()` to obtain the chromosome information.
    For each filename in the list, it reads the corresponding h5ad file and performs infercnv analysis for the genes
    with specific loss in the `allres.csv`. Also applies a filter to only plot genes passing the cell count threshold.
    Heatmaps are generated to visualize the CNV values and specific block numbers are marked on the heatmaps.
    The resulting plots are saved as SVG files with filenames corresponding to the original filenames and displayed.

    The function depends on several helper functions, such as _get_chromosome_info(),  _get_short_filename(),
    _get_infercnv_result_file(), and _get_mid_ticks().

    Args:
        filenames (List[str]): List of filenames to process.
        chromosome_info (pd.DataFrame, optional): DataFrame containing chromosome information. Defaults to None.
        blocksize (int, optional): Block size for infercnv analysis. Defaults to 5.
        window (int, optional): Window size for infercnv analysis. Defaults to 100.
        neigh (int, optional): Number of neighboring genes to consider in loss computation. Defaults to 150.

    Returns:
        None

    Raises:
        FileNotFoundError or KeyError
    """
    if chromosome_info is None:
        chromosome_info = _get_chromosome_info()
    allres = pd.read_csv(get_specific_loss_file_path())
    sns.set(font_scale=1.7)
    plt.rcParams["svg.fonttype"] = "none"
    for filename in filenames:
        ad = read_and_log_transform_h5ad_file(os.path.join(str(utils.constants.DATA_DIR), f"{filename}.h5ad"))
        filename_short = _get_short_filename(filename)
        perts2check_df = allres[
            (allres["Dataset"] == filename_short)
            & (allres["# affected cells"] >= _get_cell_count_threshold(filename_short))
        ]
        perts2check = sorted(set(perts2check_df["Perturbed gene"]))

        ad.var = ad.var.rename(columns={"start": "st", "end": "en"}).join(chromosome_info, how="left")
        if filename_short == "Papalexi":
            ad.obs["gene"] = (
                ad.obs.perturbation.apply(lambda x: x.split("g")[0] if x != "control" else "").fillna("").astype(str)
            )
        elif filename_short == "Frangieh":
            ad.obs["gene"] = ad.obs.perturbation.apply(lambda x: x if x != "control" else "").fillna("").astype(str)
        ad.obs.loc[ad.obs.perturbation == "control", "gene"] = "control"
        ad = ad[ad.obs.gene.isin(perts2check + ["control"])]
        infercnvpy.tl.infercnv(
            ad,
            reference_key="gene",
            reference_cat="control",
            window_size=window,
            step=blocksize,
            exclude_chromosomes=None,
        )
        res = pd.read_csv(_get_infercnv_result_file_path(filename, blocksize, window, neigh))
        loss_arrs: List[float] = []
        other_arrs: List[float] = []
        loss_seps: List[int] = []
        other_seps: List[int] = []
        blocknums = []
        for p in perts2check:
            res_p = res[(res.ko_gene == p) & (res.aff_gene == p)]
            direcs = list(perts2check_df[perts2check_df["Perturbed gene"] == p]["Tested loss direction"])
            loss_cell_inds: List[str] = sum(
                [[ad.obs.index.get_loc(x) for x in literal_eval(res_p[f"loss{d[0]}p_cells"].iloc[0])] for d in direcs],
                [],
            )
            other_cell_inds = list(
                set(ad.obs.index.get_loc(x) for x in ad.obs.loc[ad.obs.gene == p].index).difference(loss_cell_inds)
            )
            arr1 = ad.obsm["X_cnv"].toarray()[loss_cell_inds]
            arr2 = ad.obsm["X_cnv"].toarray()[other_cell_inds]
            aff_chr = ad.var.loc[p].chromosome
            aff_chr_startblocknum = ad.uns["cnv"]["chr_pos"][aff_chr]
            blocknum_p = (
                aff_chr_startblocknum
                + ad.var[ad.var.chromosome == aff_chr].sort_values("start").index.get_loc(p) // blocksize
            )
            blocknums.append(blocknum_p)
            loss_arrs = arr1 if len(loss_arrs) == 0 else np.concatenate((loss_arrs, arr1), axis=0)
            other_arrs = arr2 if len(other_arrs) == 0 else np.concatenate((other_arrs, arr2), axis=0)
            loss_seps.append(len(loss_cell_inds) if len(loss_seps) == 0 else len(loss_cell_inds) + loss_seps[-1])
            other_seps.append(len(other_cell_inds) if len(other_seps) == 0 else len(other_cell_inds) + other_seps[-1])

        crunch = _get_crunch_size(filename_short)
        plt.figure(figsize=[20, int(loss_seps[-1] / 15)])  # type: ignore[arg-type]
        tmp = block_reduce(loss_arrs, (1, crunch), np.mean)
        ax = sns.heatmap(
            tmp, cmap="seismic", center=0, cbar_kws=dict(use_gridspec=False, location="top", shrink=0.5, pad=0.01)
        )
        x_tick_loc = _get_mid_ticks(list(ad.uns["cnv"]["chr_pos"].values()) + [ad.obsm["X_cnv"].shape[1]])
        x_tick_lab = list(ad.uns["cnv"]["chr_pos"].keys())
        ax.set_xticks([x / crunch for x in x_tick_loc])
        loss_seps_tmp = [0] + loss_seps
        ax.set_yticks(_get_mid_ticks(loss_seps_tmp))
        ax.set_xticklabels(x_tick_lab)
        ax.set_yticklabels(perts2check)
        ax.hlines(loss_seps_tmp, *ax.get_xlim())
        for i in range(len(blocknums)):
            ax.vlines(blocknums[i] / crunch, loss_seps_tmp[i], loss_seps_tmp[i + 1], color="lime", linewidth=3)
        for j in ad.uns["cnv"]["chr_pos"].values():
            ax.vlines(j / crunch, *ax.get_ylim())
        plt.gcf().set_facecolor("white")
        plt.savefig(f"{filename}.svg", format="svg", bbox_inches="tight")
        plt.show()
