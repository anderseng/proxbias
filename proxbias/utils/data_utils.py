from typing import List, Tuple

import pandas as pd

from proxbias.utils.constants import CANCER_GENES_FILENAME, DATA_DIR


def _get_data_path(name):
    return DATA_DIR.joinpath(name)


def get_cancer_gene_lists(valid_genes: List[str]) -> Tuple[List[str], List[str]]:
    # Get the correct path for the data file
    cancer_genes_path = _get_data_path(CANCER_GENES_FILENAME)
    
    # Read the data file
    oncokb = pd.read_csv(cancer_genes_path, delimiter="\t")
    oncokb = oncokb.loc[oncokb["Hugo Symbol"].isin(valid_genes)]
    tsg_genes = oncokb.loc[oncokb["Is Tumor Suppressor Gene"] == "Yes", "Hugo Symbol"].tolist()
    oncogenes = oncokb.loc[oncokb["Is Oncogene"] == "Yes", "Hugo Symbol"].tolist()
    return tsg_genes, oncogenes
