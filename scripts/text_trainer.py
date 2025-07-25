#!/usr/bin/env python3
"""
Standalone script for text model training (InstructText, DPO, and GRPO)
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
import yaml
from transformers import AutoTokenizer
from transformers import AutoModelForCausalLM


script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.append(project_root)

import trainer.constants as train_cst
from core.config.config_handler import create_dataset_entry
from core.config.config_handler import save_config
from core.config.config_handler import update_flash_attention
from core.dataset_utils import adapt_columns_for_dpo_dataset
from core.dataset_utils import adapt_columns_for_grpo_dataset
from core.models.utility_models import DpoDatasetType
from core.models.utility_models import FileFormat
from core.models.utility_models import GrpoDatasetType
from core.models.utility_models import InstructTextDatasetType
from core.models.utility_models import TaskType
from miner.logic.job_handler import create_reward_funcs_file

import requests
import re
KNOWN_MODEL_PARAMS = {
    "tinyllama_v1.1": 1_100_000_000,
}

def parse_param_count_from_name(model_name: str) -> int:
    if not isinstance(model_name, str):
        return 0

    for k, v in KNOWN_MODEL_PARAMS.items():
        if k.lower() in model_name.lower():
          return v

    m = re.search(r'(?i)(\d+(?:\.\d+)?)\s*([mMbB])\b', model_name)
    if not m:
        return 0

    number_str, unit = m.group(1), m.group(2).upper()
    try:
        number = float(number_str)
    except ValueError:
        return 0

    if unit == 'M':
        return int(number * 1_000_000)
    elif unit == 'B':
        return int(number * 1_000_000_000)

    return 0

def get_hf_model_param_count(model_name: str) -> int:
    api_url = f"https://huggingface.co/api/models/{model_name}"
    try:
        resp = requests.get(api_url, timeout=5)
        resp.raise_for_status()
        info = resp.json()
        total = info.get("safetensors", {}).get("total")
        if isinstance(total, (int, float)):
            return int(total)
    except Exception:
        pass

    return parse_param_count_from_name(model_name)

def patch_model_metadata(output_dir: str, base_model_id: str):
    try:
        adapter_config_path = os.path.join(output_dir, "adapter_config.json")

        if os.path.exists(adapter_config_path):
            with open(adapter_config_path, "r") as f:
                config = json.load(f)

            config["base_model_name_or_path"] = base_model_id

            with open(adapter_config_path, "w") as f:
                json.dump(config, f, indent=2)

            print(f"Updated adapter_config.json with base_model: {base_model_id}", flush=True)
        else:
            print(" adapter_config.json not found", flush=True)

        readme_path = os.path.join(output_dir, "README.md")

        if os.path.exists(readme_path):
            with open(readme_path, "r") as f:
                lines = f.readlines()

            new_lines = []
            for line in lines:
                if line.strip().startswith("base_model:"):
                    new_lines.append(f"base_model: {base_model_id}\n")
                else:
                    new_lines.append(line)

            with open(readme_path, "w") as f:
                f.writelines(new_lines)

            print(f"Updated README.md with base_model: {base_model_id}", flush=True)
        else:
            print("README.md not found", flush=True)

    except Exception as e:
        print(f"Error updating metadata: {e}", flush=True)
        pass


def copy_dataset_if_needed(dataset_path, file_format):
    """Copy dataset to Axolotl directories for non-HF datasets."""
    if file_format != FileFormat.HF.value:
        dataset_filename = os.path.basename(dataset_path)

        os.makedirs("/workspace/axolotl/data", exist_ok=True)
        os.makedirs("/workspace/axolotl", exist_ok=True)

        data_path = f"/workspace/axolotl/data/{dataset_filename}"
        root_path = f"/workspace/axolotl/{dataset_filename}"

        shutil.copy(dataset_path, data_path)
        shutil.copy(dataset_path, root_path)

        return data_path
    return dataset_path


import torch

def get_gpu_count():
    return torch.cuda.device_count()


def get_batch_size_by_model_size(model_name: str) -> int:
    count = get_hf_model_param_count(model_name)
    if count == 0:
        return 0

    params_in_billion = count / 1_000_000_000
    if params_in_billion <= 0.5:
        base_batch_size = 32
    elif params_in_billion <= 1.6:
        base_batch_size = 16
    elif params_in_billion <= 7.1:
        base_batch_size = 8
    elif params_in_billion <= 9.1:
        base_batch_size = 4
    elif params_in_billion <= 14.1:
        base_batch_size = 2
    else:
        base_batch_size = 1

    if 'qwen' in model_name.lower():
        base_batch_size = max(1, base_batch_size / 2)

    return base_batch_size


def get_learning_rate_by_batch_and_gpu(batch_size: int, gpu_count: int, base_learning_rate: float = 0.00008) -> float:
    base_gpu_count = 8
    base_batch_size = 8
    
    effective_batch_size = batch_size * gpu_count
    base_effective_batch_size = base_batch_size * base_gpu_count

    adjusted_learning_rate = base_learning_rate * (effective_batch_size / base_effective_batch_size)
    
    return adjusted_learning_rate


def create_config(task_id, model, dataset, dataset_type, file_format, output_dir, expected_repo_name=None,
                huggingface_username=None, huggingface_token=None, disable_upload=True):
    """Create the axolotl config file with appropriate settings."""
    config_path = "/workspace/axolotl/base.yml"
    with open(config_path, "r") as file:
        config = yaml.safe_load(file)

    config["datasets"] = [create_dataset_entry(dataset, dataset_type, FileFormat(file_format))]
    model_path = f"{train_cst.CACHE_PATH}/models/{model.replace('/', '--')}"
    config["base_model"] = model_path
    config["mlflow_experiment_name"] = dataset
    os.makedirs(output_dir, exist_ok=True)
    config["output_dir"] = output_dir

    config = update_flash_attention(config, model)

    if isinstance(dataset_type, DpoDatasetType):
        config["rl"] = "dpo"
    elif isinstance(dataset_type, GrpoDatasetType):
        config["save_steps"] = 20
        config["rl"] = "grpo"
        config["trl"] = {
            "beta": 0.04,
            "max_completion_length": 256,
            "use_vllm": False,
            "num_generations": 2,
        }
        filename, reward_funcs_names = create_reward_funcs_file(
            [reward_function.reward_func for reward_function in dataset_type.reward_functions],
            task_id,
            destination_dir="/workspace/axolotl/src/",
        )
        config["trl"]["reward_funcs"] = [f"{filename}.{func_name}" for func_name in reward_funcs_names]
        config["trl"]["reward_weights"] = [reward_function.reward_weight for reward_function in dataset_type.reward_functions]

    if not disable_upload:
        hf_username = huggingface_username or os.environ.get("HUGGINGFACE_USERNAME", "rayonlabs")
        os.environ["HUGGINGFACE_USERNAME"] = hf_username

        repo_name = expected_repo_name or str(uuid.uuid4())
        config["hub_model_id"] = f"{hf_username}/{repo_name}"

        if huggingface_token:
            os.environ["HUGGINGFACE_TOKEN"] = huggingface_token
    else:
        for key in list(config.keys()):
            if key.startswith("wandb") or key.startswith("hub"):
                config.pop(key)

    if file_format != FileFormat.HF.value:
        for ds in config["datasets"]:
            ds["ds_type"] = "json"

            if "path" in ds:
                ds["path"] = "/workspace/axolotl/data"

            ds["data_files"] = [os.path.basename(dataset)]

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        config["special_tokens"] = {"pad_token": tokenizer.eos_token}
        
    gpu_count = get_gpu_count()
    if gpu_count > 1:
        config["deepspeed"] = "zero2.json"
    batch_size = get_batch_size_by_model_size(model)
    if batch_size:
        config["micro_batch_size"] = batch_size
        config["learning_rate"] = get_learning_rate_by_batch_and_gpu(batch_size, gpu_count, config["base_learning_rate"])

    config_path = os.path.join("/workspace/axolotl/configs", f"{task_id}.yml")
    save_config(config, config_path)
    return config_path


def run_training(config_path):
    print(f"Starting training with config: {config_path}", flush=True)
    """Run the training process using the specified config file."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    training_env = os.environ.copy()
    training_env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    training_env["HF_HUB_DISABLE_TELEMETRY"] = "1"

    training_command = [
    "accelerate", "launch",
    "-m", "axolotl.cli.train",
    config_path
    ]

    try:
        print("Starting training subprocess...\n", flush=True)
        process = subprocess.Popen(
            training_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in process.stdout:
            print(line, end="", flush=True)

        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, training_command)

        print("Training subprocess completed successfully.", flush=True)

    except subprocess.CalledProcessError as e:
        print("Training subprocess failed!", flush=True)
        print(f"Exit Code: {e.returncode}", flush=True)
        print(f"Command: {' '.join(e.cmd) if isinstance(e.cmd, list) else e.cmd}", flush=True)
        raise RuntimeError(f"Training subprocess failed with exit code {e.returncode}")



async def main():
    print("---STARTING TEXT TRAINING SCRIPT---", flush=True)
    parser = argparse.ArgumentParser(description="Text Model Training Script")
    parser.add_argument("--task-id", required=True, help="Task ID")
    parser.add_argument("--model", required=True, help="Model name or path")
    parser.add_argument("--dataset", required=True, help="Dataset path or HF dataset name")
    parser.add_argument("--dataset-type", required=True, help="JSON string of dataset type config")
    parser.add_argument("--task-type", required=True, choices=["InstructTextTask", "DpoTask", "GrpoTask"], help="Type of task")
    parser.add_argument("--file-format", required=True, choices=["csv", "json", "hf", "s3"], help="File format")
    parser.add_argument("--hours-to-complete", type=float, required=True, help="Number of hours to complete the task")
    parser.add_argument("--expected-repo-name", help="Expected repository name")
    args = parser.parse_args()

    for directory in [
        "/workspace/axolotl/data",
        "/workspace/axolotl/data_prepared",
        "/workspace/axolotl/configs",
        "/workspace/axolotl/outputs",
        "/workspace/input_data",
        "/workspace/axolotl"
    ]:
        os.makedirs(directory, exist_ok=True)
    try:
        dataset_type_dict = json.loads(args.dataset_type)

        if args.task_type == TaskType.DPOTASK.value:
            dataset_type = DpoDatasetType(**dataset_type_dict)
        elif args.task_type == TaskType.INSTRUCTTEXTTASK.value:
            dataset_type = InstructTextDatasetType(**dataset_type_dict)
        elif args.task_type == TaskType.GRPOTASK.value:
            dataset_type = GrpoDatasetType(**dataset_type_dict)
        else:
            sys.exit(f"Unsupported task type: {args.task_type}")
    except Exception as e:
        sys.exit(f"Error creating dataset type object: {e}")

    base_dataset_path = f"{train_cst.CACHE_PATH}/datasets"
    dataset_path = f"{base_dataset_path}/{args.task_id}_train_data.json" if args.file_format == FileFormat.S3.value else f"{base_dataset_path}/{args.dataset.replace('/', '--')}"

    if args.file_format == FileFormat.S3.value and args.task_type == TaskType.DPOTASK.value:
        adapt_columns_for_dpo_dataset(dataset_path, dataset_type, apply_formatting=True)
    elif args.file_format == FileFormat.S3.value and args.task_type == TaskType.GRPOTASK.value:
        adapt_columns_for_grpo_dataset(dataset_path, dataset_type)

    dataset_path = copy_dataset_if_needed(dataset_path, args.file_format)

    output_dir = f"/workspace/axolotl/outputs/{args.task_id}/{args.expected_repo_name}"

    config_path = create_config(
        args.task_id,
        args.model,
        dataset_path,
        dataset_type,
        args.file_format,
        output_dir,
        args.expected_repo_name,
    )

    run_training(config_path)

    patch_model_metadata(output_dir, args.model)


if __name__ == "__main__":
    asyncio.run(main())
