import argparse
from matplotlib import pyplot as plt
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
import math
from accelerate import Accelerator
from ema_pytorch import EMA
from torch.optim import Adam
from pathlib import Path
from tqdm.auto import tqdm
import os
import sys
import yaml
from torch.utils.data import DataLoader, TensorDataset
from torch.optim.lr_scheduler import StepLR

# import path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from filepath import ABSOLUTE_PATH

sys.path.append(ABSOLUTE_PATH)
from src.model.diffusion import GaussianDiffusion
from src.model.UNet2d import Unet2D
from src.train.train import Trainer
from src.utils.utils import create_res, set_seed, get_time, save_config_from_args, get_parameter_net, find_max_min
import time


def cond_emb():

    def consistent(x):
        return x

    return [consistent]


def normalize_to_neg_one_to_one(x):
    return (x + 5.0) / 10.0 * 2 - 1


def renormalize(x):
    return (x + 1.0) * 10.0 / 2 - 5


def forward_function(paradigm):
    if paradigm == "surrogate":

        def func(model: nn.Module, batch, loss_fn):
            data, *cond = batch
            outputs_p = model(cond)
            loss = loss_fn(data, outputs_p)
            return loss

        return func, func
    elif paradigm == "diffusion":

        def func_train(model: nn.Module, batch, loss_fn=F.mse_loss):
            data, *cond = batch
            loss = model(data, cond)
            return loss

        def func_val(model: nn.Module, batch, loss_fn=F.mse_loss):
            data, *cond = batch
            batchsize = data.shape[0]
            outputs_p = model.sample(batchsize, cond)
            loss = loss_fn(data, outputs_p)
            return loss

        return func_train, func_val
    else:
        raise Exception("paradigm is not exist")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train simple distribution model")
    parser.add_argument(
        "--exp_id", default="reaction_diffusion_couple_model_24_continue", type=str, help="experiment folder id"
    )
    parser.add_argument("--train_which", default="v", type=str, help="train u or v")
    parser.add_argument("--lr", default=1e-4, type=float, help="learning rate")
    parser.add_argument("--batchsize", default=256, type=int, help="size of dataset")
    parser.add_argument("--epoches", default=110000, type=int, help="training epoch")
    parser.add_argument("--diffusion_step", default=250, type=int, help="diffusion_step")
    parser.add_argument("--checkpoint", default=2000, type=int, help="save and sample period")
    parser.add_argument("--overall_results_path", default="results", type=str, help="where to create result folder")
    parser.add_argument("--seed", default=42, type=int, help="random seed")
    parser.add_argument("--in_dim", default=2, type=int, help="channel")
    parser.add_argument("--dim", default=24, type=int, help="encode dim")
    parser.add_argument("--nx", default=20, type=int, help="dim in space")
    parser.add_argument("--out_dim", default=2, type=int, help="cond channel")
    parser.add_argument("--gap", default=9000, type=int, help="dataset size for train")
    parser.add_argument("--n_dataset", default=10000, type=int, help="n of data")
    parser.add_argument("--paradigm", default="diffusion", type=str, help="diffusion or surrogate")
    parser.add_argument("--model_type", default="Unet", type=str, help="Unet or ViT or FNO")
    parser.add_argument("--network_dim", default=2, type=int, help="1 or 2")
    parser.add_argument("--gradient_accumulate_every", default=2, type=int, help="gradient_accumulate_every")
    # ViT
    parser.add_argument("--patch_size", default=(2, 4), type=tuple, help="patch size")
    parser.add_argument("--depth", default="2", type=int, help="transformer depth")
    parser.add_argument("--head", default="4", type=int, help="head of attention")
    parser.add_argument("--vit_dim", default="64", type=int, help="vit dim")
    parser.add_argument("--mlp_dim", default="64", type=int, help="vit mlp dim")
    # FNO
    parser.add_argument("--fno_nlayer", default=4, type=int, help="fno layers")
    parser.add_argument("--fno_layer_size", default=24, type=int, help="fno_layer_size")
    parser.add_argument("--fno_modes", default=[6, 12], type=list, help="fno_modes")
    args = parser.parse_args()

    set_seed(args.seed)
    results_path = create_res(path=os.path.join(ABSOLUTE_PATH, args.overall_results_path), folder_name=args.exp_id)
    train_which = args.train_which
    paradigm = args.paradigm
    model_type = args.model_type
    results_folder = os.path.join(results_path, paradigm + model_type + train_which + str(args.n_dataset))
    if not os.path.exists(results_folder):
        os.makedirs(results_folder)
    save_config_from_args(args, results_folder)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    uv = torch.tensor(np.load(ABSOLUTE_PATH + "/data/reaction_diffusion/reaction_diffusion_uv.npy").transpose(0, 2, 1))
    # uv = uv
    u = uv[..., :20].unsqueeze(1)
    v = uv[..., 20:].unsqueeze(1)
    data = torch.concat((u, v), dim=1)
    cond = torch.concat((u[:, 0:1], v[:, 0:1]), axis=1).expand(-1, -1, data.shape[2], -1)
    # u0 is cond
    data, cond = (
        torch.tensor(normalize_to_neg_one_to_one(data[: args.n_dataset])).float().to(device),
        torch.tensor(normalize_to_neg_one_to_one(cond[: args.n_dataset])).float().to(device),
    )
    interval = args.gap
    train_dataset = TensorDataset(data[:interval], cond[:interval])
    test_dataset = TensorDataset(data[interval:], cond[interval:])
    train_function, val_function = forward_function(paradigm)
    if paradigm == "diffusion":
        if model_type == "Unet":
            model = Unet2D(
                dim=args.dim,
                cond_emb=cond_emb(),
                out_dim=args.out_dim,
                dim_mults=(1, 2),
                channels=args.in_dim + args.out_dim,
            )
        elif model_type == "ViT":
            model = ViT(
                seq_len=args.nx,
                patch_size=args.patch_size,
                dim=args.vit_dim,
                depth=args.depth,
                heads=args.head,
                mlp_dim=args.mlp_dim,
                cond_emb=cond_emb(),
                dropout=0.1,
                emb_dropout=0.1,
                Time_Input=True,
                channels=args.in_dim + args.out_dim,
                out_channels=args.out_dim,
            )
        diffusion = GaussianDiffusion(
            model, seq_length=(args.out_dim, 10, args.nx), timesteps=args.diffusion_step, auto_normalize=False
        )
        diffusion.load_state_dict(
            torch.load(ABSOLUTE_PATH + "/results/reaction_diffusion_couple_model_24/diffusionUnetv10000/model-42.pt")[
                "model"
            ]
        )
        get_parameter_net(diffusion)
        train = Trainer(
            model=diffusion,
            data_train=train_dataset,
            data_val=test_dataset,
            train_function=train_function,
            val_function=val_function,
            train_lr=args.lr,
            train_num_steps=args.epoches,
            train_batch_size=args.batchsize,
            save_every=args.checkpoint,
            results_folder=results_folder,
            gradient_accumulate_every=args.gradient_accumulate_every,
        )
        train.train()