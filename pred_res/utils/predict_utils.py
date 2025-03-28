"""
File: predict_utils.py
Author: Koustav Mallick
Date: 06/11/2024

Description: [Add a brief description of the file here]
"""

import logging
import math
import os
import warnings

import numpy as np
import pandas as pd
import torch
from scipy.io import loadmat
from torch import nn

import model
from dataset.ECGData import resample, prepare_data
from utils.datasplit import ls_dir

logger = logging.getLogger(__name__)

if torch.cuda.is_available():
    device = torch.device("cuda")
    device_count = torch.cuda.device_count()
else:
    warnings.warn("gpu is not available")
    device = torch.device("cpu")


def processing_data(data, win_length, src_fs, tar_fs):
    """
    Preprocesses ECG data for classification.

    Args:
        data (numpy.ndarray): Raw ECG data.
        win_length (int): Window length for processing.
        src_fs (int): Source sampling frequency.
        tar_fs (int): Target sampling frequency.

    Returns:
        torch.Tensor: Processed ECG data as a tensor.
    """
    data = resample(data, src_fs, tar_fs)
    num = data.shape[1]
    if num < win_length:
        zeros_padding = np.zeros(shape=(data.shape[0], win_length - num), dtype=np.float32)
        data = np.hstack((data, zeros_padding))
    data = data.astype(np.float32)
    data = torch.from_numpy(data)
    data = torch.unsqueeze(data, 0)

    return data


def read_ag(header_data):
    """
    Reads the age and gender information from ECG header data.

    Args:
        header_data (list): List of lines from the ECG header.

    Returns:
        torch.Tensor: Processed age and gender data as a tensor.
    """
    for lines in header_data:
        if lines.startswith('#Age'):
            tmp = lines.split(': ')[1].strip()
            if tmp == 'NaN':
                age = -1
            else:
                age = int(tmp)
        if lines.startswith('#Sex'):
            tmp = lines.split(': ')[1].strip()
            if tmp == 'NaN':
                gender = 'Unknown'
            else:
                gender = tmp
    data = prepare_data(age, gender)
    data = torch.from_numpy(data).float()
    data = torch.unsqueeze(data, 0)
    return data


def run_classifier(data, header_data, model_name):
    """
    Runs the classifier on the ECG data.

    Args:
        data (numpy.ndarray): ECG data.
        header_data (list): List of lines from the ECG header.
        model_name (torch.nn.Module): Trained model for classification.

    Returns:
        tuple: Predicted labels, scores, and classes.
    """
    A = np.load('./magic_weight.npz')
    threshold = A['arr_0']
    num_classes = 24
    tar_fs = 257
    src_fs = int(header_data[0].split(' ')[2].strip())
    ag = read_ag(header_data)
    ag = ag.to(device)

    win_length = 4096
    current_label = np.zeros(num_classes, dtype=int)

    m = nn.Sigmoid()
    data = processing_data(data, win_length, src_fs, tar_fs)
    inputs = data.to(device)

    val_length = inputs.shape[2]
    overlap = 256
    patch_number = math.ceil(abs(val_length - win_length) / (win_length - overlap)) + 1
    if patch_number > 1:
        start = int((val_length - win_length) / (patch_number - 1))
    prob = 0
    logits_prob = 0
    for j in range(len(model_name)):
        model_one = model_name[j]
        for i in range(patch_number):
            if i == 0:
                logit = model_one(inputs[:, :, 0: val_length], ag)
                logits_prob = m(logit)
            elif i == patch_number - 1:
                logit = model_one(inputs[:, :, val_length - win_length: val_length], ag)
                logits_prob_tmp = m(logit)
                logits_prob = (logits_prob + logits_prob_tmp) / patch_number
            else:
                logit = model_one(inputs[:, :, i * start:i * start + win_length], ag)
                logits_prob_tmp = m(logit)
                logits_prob = logits_prob + logits_prob_tmp

        prob = prob + logits_prob

    logits_prob = prob / len(model_name)

    _, y_pre_label = torch.max(logits_prob, 1)
    y_pre_label = y_pre_label.cpu().detach().numpy()
    current_label[y_pre_label] = 1

    score = logits_prob.cpu().detach().numpy()

    y_pre = (score - threshold) >= 0
    current_label = current_label + y_pre
    current_label[current_label > 1.1] = 1

    current_label = np.squeeze(current_label)
    current_score = np.squeeze(score)

    # Get the label
    label_file_dir = './utils/dx_mapping_scored.csv'
    label_file = pd.read_csv(label_file_dir)
    equivalent_classes = ['59118001', '63593006', '17338001']
    classes = sorted(list(set([str(name) for name in label_file['SNOMED CT Code']]) - set(equivalent_classes)))

    return current_label, current_score, classes


def load_model(model_input, model_base, test_model=None):
    """
    Loads the trained model for classification.

    Args:
        model_input (str): Path to the model directory.
        model_base (str): Base model name.
        test_model (str, optional): Name of the model to test. Defaults to None.

    Returns:
        list: List of loaded model instances.
    """
    model_list = ls_dir(rootdir=model_input, suffix=".pth")

    logger.info("Model List: {}".format(model_list))
    if test_model is None:
        # Select the model with highest accuracy
        accuracy = np.array([float(i.split('-')[-1].replace('.pth', '')) for i in model_list])
        logger.info("Model Accuracy: {}".format(accuracy))
        resumes = [model_list[int(np.argmax(accuracy))]]
    else:
        if os.path.join(model_input, test_model) in model_list:
            resumes = [os.path.join(model_input, test_model)]
        else:
            logger.error(f"Model {test_model} not found in the model list.")
            resumes = []

    logger.info("Model Path: {}".format(resumes))
    model_all = []

    for resume in resumes:
        model_name = getattr(model, model_base)(in_channel=12, out_channel=24)
        # Consider the gpu or cpu condition
        if torch.cuda.is_available():
            if device_count > 1:
                model_name = torch.nn.DataParallel(model_name)
            model_name.load_state_dict(torch.load(resume, weights_only=True), strict=False)
        else:
            model_name.load_state_dict(torch.load(resume, weights_only=True, map_location=device), strict=False)

        model_name.to(device)
        model_name.eval()
        model_all.append(model_name)

    return model_all


def load_test_data(filename):
    """
    Loads the ECG test data from a file.

    Args:
        filename (str): Path to the ECG test data file.

    Returns:
        tuple: ECG data and header information.
    """
    x = loadmat(filename)
    data = np.asarray(x['val'], dtype=np.float64)

    new_file = filename.replace('.mat', '.hea')
    input_header = os.path.join(new_file)

    with open(input_header, 'r') as f:
        header_data = f.readlines()

    return data, header_data


def json_serializer(obj):
    """
    Recursively converts numpy types and other non-JSON-serializable objects
    into JSON-compatible types.
    """
    if isinstance(obj, dict):
        return {k: json_serializer(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [json_serializer(v) for v in obj]
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    else:
        return obj