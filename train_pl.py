import os
import torch
import logging
import argparse
from pytorch_lightning.callbacks import Callback
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.loggers import TensorBoardLogger
from glob import glob
from models import get_models
from datasets import get_dataset
from diffusion import create_diffusion
from omegaconf import OmegaConf
from torch.utils.data import DataLoader
from diffusers.models import AutoencoderKL
from diffusers.optimization import get_scheduler
from copy import deepcopy
from einops import rearrange
from utils import (
    update_ema,
    requires_grad,
    get_experiment_dir,
    clip_grad_norm_,
    cleanup,
)


class LatteTrainingModule(LightningModule):
    def __init__(self, args, logger: logging.Logger):
        super(LatteTrainingModule, self).__init__()
        self.args = args
        self.logging = logger
        self.model = get_models(args)
        self.ema = deepcopy(self.model)
        requires_grad(self.ema, False)

        # Load pretrained model if specified
        if args.pretrained and args.resume_from_checkpoint is not None:
            # Load old checkpoint, only load EMA
            self._load_pretrained_parameters(args)
        self.logging.info(f"Model Parameters: {sum(p.numel() for p in self.model.parameters()):,}")

        self.diffusion = create_diffusion(timestep_respacing="")
        self.vae = AutoencoderKL.from_pretrained(args.pretrained_model_path, subfolder="vae")
        self.opt = torch.optim.AdamW(self.model.parameters(), lr=1e-4, weight_decay=0)
        self.lr_scheduler = None

        # Freeze VAE
        self.vae.requires_grad_(False)

        update_ema(self.ema, self.model, decay=0)  # Ensure EMA is initialized with synced weights
        self.model.train()  # important! This enables embedding dropout for classifier-free guidance
        self.ema.eval()

    def _load_pretrained_parameters(self, args):
        checkpoint = torch.load(args.pretrained, map_location=lambda storage, loc: storage)
        if "ema" in checkpoint:  # supports checkpoints from train.py
            self.logging.info("Using ema ckpt!")
            checkpoint = checkpoint["ema"]

        model_dict = self.model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {}
        for k, v in checkpoint.items():
            if k in model_dict:
                pretrained_dict[k] = v
            else:
                self.logging.info("Ignoring: {}".format(k))
        self.logging.info(f"Successfully Load {len(pretrained_dict) / len(checkpoint.items()) * 100}% original pretrained model weights ")

        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrained_dict)
        self.model.load_state_dict(model_dict)
        self.logging.info(f"Successfully load model at {args.pretrained}!")

        # self.global_step = int(args.pretrained.split("/")[-1].split(".")[0])    # dirty implementation

    def training_step(self, batch, batch_idx):
        x = batch["video"].to(self.device)
        video_name = batch["video_name"]

        with torch.no_grad():
            b, _, _, _, _ = x.shape
            x = rearrange(x, "b f c h w -> (b f) c h w").contiguous()
            x = self.vae.encode(x).latent_dist.sample().mul_(0.18215)
            x = rearrange(x, "(b f) c h w -> b f c h w", b=b).contiguous()

        model_kwargs = dict(y=video_name if self.args.extras == 2 else None)

        t = torch.randint(0, self.diffusion.num_timesteps, (x.shape[0],), device=self.device)
        loss_dict = self.diffusion.training_losses(self.model, x, t, model_kwargs)
        loss = loss_dict["loss"].mean()

        if self.global_step < self.args.start_clip_iter:
            gradient_norm = clip_grad_norm_(self.model.parameters(), self.args.clip_max_norm, clip_grad=False)
        else:
            gradient_norm = clip_grad_norm_(self.model.parameters(), self.args.clip_max_norm, clip_grad=True)

        self.log("train_loss", loss)
        self.log("gradient_norm", gradient_norm)

        if (self.global_step+1) % self.args.log_every == 0:
            self.logging.info(
                f"(step={self.global_step+1:07d}/epoch={self.current_epoch:04d}) Train Loss: {loss:.4f}, Gradient Norm: {gradient_norm:.4f}"
            )
        return loss

    def on_train_batch_end(self, *args, **kwargs):
        update_ema(self.ema, self.model)

    def configure_optimizers(self):
        self.lr_scheduler = get_scheduler(
            name="constant",
            optimizer=self.opt,
            num_warmup_steps=self.args.lr_warmup_steps * self.args.gradient_accumulation_steps,
            num_training_steps=self.args.max_train_steps * self.args.gradient_accumulation_steps,
        )
        return [self.opt], [self.lr_scheduler]


def create_logger(logging_dir):
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(f"{logging_dir}/log.txt")]
    )
    logger = logging.getLogger(__name__)
    return logger


def create_experiment_directory(args):
    os.makedirs(args.results_dir, exist_ok=True)        # Make results folder (holds all experiment subfolders)
    experiment_index = len(glob(os.path.join(args.results_dir, "*")))
    model_string_name = args.model.replace("/", "-")    # e.g., Latte-XL/2 --> Latte-XL-2 (for naming folders)
    num_frame_string = f"F{args.num_frames}S{args.frame_interval}"
    experiment_dir = os.path.join(                      # Create an experiment folder
        args.results_dir,
        f"{experiment_index:03d}-{model_string_name}-{num_frame_string}-{args.dataset}"
    )
    experiment_dir = get_experiment_dir(experiment_dir, args)
    checkpoint_dir = os.path.join(experiment_dir, "checkpoints")    # Stores saved model checkpoints
    os.makedirs(checkpoint_dir, exist_ok=True)

    return experiment_dir, checkpoint_dir


def main(args):
    seed = args.global_seed
    torch.manual_seed(seed)

    # Setup an experiment folder and logger
    experiment_dir, checkpoint_dir = create_experiment_directory(args)
    logger = create_logger(experiment_dir)
    tb_logger = TensorBoardLogger(experiment_dir, name="latte")
    OmegaConf.save(args, os.path.join(experiment_dir, "config.yaml"))
    logger.info(f"Experiment directory created at {experiment_dir}")

    # Create the dataset and dataloader
    dataset = get_dataset(args)
    loader = DataLoader(
        dataset,
        batch_size=args.local_batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True
    )
    logger.info(f"Dataset contains {len(dataset):,} videos ({args.data_path})")

    sample_size = args.image_size // 8
    args.latent_size = sample_size

    # Initialize the training module
    pl_module = LatteTrainingModule(args, logger)

    checkpoint_callback = ModelCheckpoint(
        dirpath=checkpoint_dir,
        filename="{epoch}-{step}-{train_loss:.2f}-{gradient_norm:.2f}",
        save_top_k=-1,
        every_n_train_steps=args.ckpt_every,
        save_on_train_epoch_end=True,       # Optional
    )

    # Trainer
    trainer = Trainer(
        accelerator="gpu",
        # devices=[3],    # Specify GPU ids
        strategy="auto",
        max_steps=args.max_train_steps,
        logger=tb_logger,
        callbacks=[checkpoint_callback, LearningRateMonitor()],
        log_every_n_steps=args.log_every,
    )

    trainer.fit(pl_module, train_dataloaders=loader, ckpt_path=args.resume_from_checkpoint if args.resume_from_checkpoint else None)

    pl_module.model.eval()  # important! This disables randomized embedding dropout
    # do any sampling/FID calculation/etc. with ema (or model) in eval mode ...
    logger.info("Done!")
    cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./configs/ffs/ffs_train.yaml")
    args = parser.parse_args()
    main(OmegaConf.load(args.config))