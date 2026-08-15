"""Minimal shim replacing ComfyUI's folder_paths for standalone use.

The node modules import `folder_paths` at module level and use
`folder_paths.models_dir` + `add_model_folder_path`. This module provides
just enough surface to let them run without ComfyUI installed.
"""

import os

models_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

supported_pt_extensions = set()

def add_model_folder_path(folder_name, full_folder_path, is_default=False):
    pass

def get_full_path(folder_name, filename):
    return None

def get_filename_list(folder_name):
    return []

def get_full_path_or_raise(folder_name, filename):
    raise FileNotFoundError(filename)

def get_temp_directory():
    return os.path.join(models_dir, "..", "temp")

def get_input_directory():
    return os.path.join(models_dir, "..", "input")

def get_annotated_filepath(filename):
    return filename

def exists_annotated_filepath(filename):
    return os.path.exists(filename)