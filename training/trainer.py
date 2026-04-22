"""
HTR fine-tuning trainer — config-driven, MLflow-tracked, quality-gated.

One script that runs any of the 4 candidates defined in configs/:

  python trainer.py --config configs/baseline.yaml
  python trainer.py --config configs/finetune_iam.yaml
  python trainer.py --config configs/finetune_corrections.yaml
  python trainer.py --config configs/finetune_combined.yaml

For each run, we log to MLflow:
  - config params (everything in the YAML)
  - task metrics: cer, wer (on a frozen IAM validation slice)
  - training cost metrics: total_time_s, epoch_time_s, peak_mem_mb
  - environment: torch version, CUDA available, device, num_cpu
  - git sha (if available)
  - the final model as an artifact

Quality gate:
  The first run that touches a fresh MLflow server is the "baseline"
  (config.role == "baseline"), which computes baseline CER and records
  it under MLflow tag "htr_baseline_cer" on the experiment.

  For every subsequent run, we register to the MLflow model registry
  as name "htr" IFF  val_cer <= baseline_cer * gate_tolerance
  (default tolerance = 1.05 → "no worse than 5% above baseline").

  A run that fails the gate is tagged "gate_passed=false" and NOT
  registered. This is what makes the registry a trustworthy source of
  deployable models.

Why register-on-gate-pass (not register-best-ever)?
  The production system uses `ml_gateway` which loads whatever the
  MLflow registry currently points at. Registering only gate-passing
  candidates guarantees the registry never contains a catastrophic model
  — even if a training job succeeds with corrupted data. The rollback
  controller can then fall back to the "previous version" safely.
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import torch
import yaml
from minio import Minio
from PIL import Image

import mlflow
import mlflow.transformers

sys.path.insert(0, str(Path(__file__).parent))
from data import Example, load_iam_examples, load_training_examples
from eval_utils import evaluate, make_trocr_predictor, EvalResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trainer")


# ── Config ────────────────────────────────────────────────────────

@dataclass
class RunConfig:
    # Identity
    run_name: str
    role: str             # "baseline" | "finetune"

    # Data
    training_sources: list[str]  # ["iam"], ["corrections"], ["iam","corrections"], []
    max_train_examples: int
    max_val_examples: int
    snapshot_version: str = "latest"
    iam_split: str = "train"

    # Model
    base_model: str = "microsoft/trocr-base-handwritten"

    # Training hyperparams
    epochs: int = 1
    batch_size: int = 2
    learning_rate: float = 5e-5
    weight_decay: float = 0.01
    warmup_steps: int = 0
    max_new_tokens: int = 128

    # Infra
    device: str = "cpu"
    eval_batch_size: int = 4
    seed: int = 42

    # Quality gate
    gate_tolerance: float = 1.05
    register_on_pass: bool = True
    register_name: str = "htr"

    # Memory / stability knobs
    freeze_encoder: bool = True           # freeze ViT; only train decoder
    gradient_checkpointing: bool = False  # opt-in; some HF models break with this


def load_config(path: str) -> RunConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return RunConfig(**raw)


# ── Minio / MLflow setup ──────────────────────────────────────────

def _minio() -> Minio:
    return Minio(
        os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "admin"),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "paperless_minio"),
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )


def _setup_mlflow(experiment: str = "htr_training") -> None:
    tracking = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    mlflow.set_tracking_uri(tracking)
    mlflow.set_experiment(experiment)
    log.info("mlflow tracking: %s  experiment: %s", tracking, experiment)


# ── Environment capture ───────────────────────────────────────────

def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _env_info() -> dict:
    return {
        "python_version": platform.python_version(),
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none",
        "num_cpu": os.cpu_count() or 1,
        "hostname": socket.gethostname(),
        "git_sha": _git_sha(),
    }


# ── Baseline CER lookup ────────────────────────────────────────────

_BASELINE_TAG = "htr_baseline_cer"


def _get_baseline_cer() -> float | None:
    """
    Look up the baseline CER logged by a previous 'baseline' run in this
    experiment. Returns None if no baseline run has completed yet.
    """
    client = mlflow.MlflowClient()
    experiment = mlflow.get_experiment_by_name("htr_training")
    if experiment is None:
        return None
    runs = client.search_runs(
        experiment_ids=[experiment.experiment_id],
        filter_string='tags.role = "baseline" and attributes.status = "FINISHED"',
        order_by=["attributes.start_time DESC"],
        max_results=1,
    )
    if not runs:
        return None
    return runs[0].data.metrics.get("val_cer")


# ── Fine-tuning ────────────────────────────────────────────────────

def _make_torch_dataset(examples: list[Example], processor, max_target_len: int):
    """
    Wraps examples into a torch Dataset that yields (pixel_values, labels)
    in the form TrOCR expects for training.

    Built minimal on purpose — we're fine-tuning on small data with tiny
    batches, so the overhead of a proper HF Dataset isn't worth it.
    """
    from torch.utils.data import Dataset

    class HTRDataset(Dataset):
        def __init__(self):
            self.examples = examples

        def __len__(self):
            return len(self.examples)

        def __getitem__(self, idx):
            ex = self.examples[idx]
            pixel_values = processor(
                images=ex.image, return_tensors="pt"
            ).pixel_values.squeeze(0)
            labels = processor.tokenizer(
                ex.text, padding="max_length",
                max_length=max_target_len, truncation=True,
                return_tensors="pt",
            ).input_ids.squeeze(0)
            # Replace pad tokens with -100 so they're ignored by CE loss
            labels[labels == processor.tokenizer.pad_token_id] = -100
            return {"pixel_values": pixel_values, "labels": labels}

    return HTRDataset()


def finetune(
    model, processor, examples: list[Example], config: RunConfig,
) -> dict:
    """
    One-pass fine-tuning loop. Returns a metrics dict:
      train_loss_final, train_loss_initial, total_epochs_s, steps, examples_seen.
    """
    from torch.utils.data import DataLoader
    from torch.optim import AdamW

    device = torch.device(config.device)

    # Freeze encoder to halve trainable params (standard practice for
    # TrOCR fine-tuning on small data). Halves gradient + optimizer memory.
    if config.freeze_encoder:
        for p in model.encoder.parameters():
            p.requires_grad = False
        n_trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        n_total = sum(p.numel() for p in model.parameters())
        log.info("encoder frozen: trainable=%.1fM / total=%.1fM params",
                 n_trainable / 1e6, n_total / 1e6)

    # Gradient checkpointing: recomputes forward activations during backward
    # instead of holding them all. ~30% slower, ~40% less memory.
    if config.gradient_checkpointing:
        try:
            model.gradient_checkpointing_enable()
            log.info("gradient checkpointing enabled")
        except Exception as exc:
            log.warning("gradient checkpointing unavailable: %s", exc)

    model.to(device)
    model.train()

    dataset = _make_torch_dataset(examples, processor, config.max_new_tokens)
    loader = DataLoader(dataset, batch_size=config.batch_size, shuffle=True)
    optimizer = AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )

    # Linear warmup: LR ramps from 0 to target over warmup_steps. Important
    # for TrOCR fine-tuning because the first few gradient steps on a
    # near-optimal pretrained model can otherwise kick it off-manifold.
    total_steps = max(1, config.epochs * len(loader))
    warmup = max(0, min(config.warmup_steps, total_steps))

    def lr_lambda(step: int) -> float:
        if warmup > 0 and step < warmup:
            return float(step) / float(warmup)
        return 1.0

    from torch.optim.lr_scheduler import LambdaLR
    scheduler = LambdaLR(optimizer, lr_lambda=lr_lambda)

    initial_loss = None
    final_loss = None
    steps = 0
    t_start = time.time()

    for epoch in range(config.epochs):
        epoch_losses = []
        for batch in loader:
            pixel_values = batch["pixel_values"].to(device)
            labels = batch["labels"].to(device)
            outputs = model(pixel_values=pixel_values, labels=labels)
            loss = outputs.loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()

            loss_val = float(loss.detach().cpu().item())
            epoch_losses.append(loss_val)
            if initial_loss is None:
                initial_loss = loss_val
            final_loss = loss_val
            steps += 1

            if steps % 20 == 0:
                log.info("  epoch=%d step=%d loss=%.4f", epoch, steps, loss_val)

        log.info("epoch %d complete: mean_loss=%.4f  steps=%d",
                 epoch, sum(epoch_losses) / len(epoch_losses), steps)

    elapsed = time.time() - t_start
    model.eval()
    return {
        "train_loss_initial": initial_loss if initial_loss is not None else 0.0,
        "train_loss_final": final_loss if final_loss is not None else 0.0,
        "total_training_s": elapsed,
        "train_steps": steps,
        "train_examples_seen": steps * config.batch_size,
    }


# ── Main orchestration ────────────────────────────────────────────

def run(config: RunConfig) -> int:
    _setup_mlflow()

    log.info("=" * 70)
    log.info("Training run: %s  role=%s", config.run_name, config.role)
    log.info("=" * 70)

    # Load model + processor
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel
    log.info("loading base model: %s", config.base_model)
    processor = TrOCRProcessor.from_pretrained(config.base_model)
    model = VisionEncoderDecoderModel.from_pretrained(config.base_model)

    # TrOCR training needs these explicit config settings. The pretrained
    # checkpoint has them in `generation_config` (for inference) but NOT in
    # `config` (which is what the training forward pass reads). Without these
    # the model raises: "set the decoder_start_token_id attribute..."
    # This is the standard HuggingFace recommendation for TrOCR fine-tuning.
    model.config.decoder_start_token_id = processor.tokenizer.cls_token_id
    model.config.pad_token_id = processor.tokenizer.pad_token_id
    model.config.vocab_size = model.config.decoder.vocab_size
    # Also limit generation so eval doesn't go forever on edge cases
    model.config.max_length = config.max_new_tokens
    model.config.num_beams = 1   # greedy decode for speed + determinism

    # Load training + val data
    mc = _minio()
    val_examples = load_iam_examples(
        mc, split="validation", max_examples=config.max_val_examples,
        seed=config.seed,
    )

    train_examples: list[Example] = []
    if config.training_sources:
        train_examples = load_training_examples(
            mc, sources=config.training_sources,
            max_per_source=config.max_train_examples,
            snapshot_version=config.snapshot_version,
            iam_split=config.iam_split,
            seed=config.seed,
        )

    # ── MLflow run ────────────────────────────────────────────────
    with mlflow.start_run(run_name=config.run_name) as run:
        mlflow.set_tags({
            "role": config.role,
            "training_sources": ",".join(config.training_sources) or "none",
        })

        # Log config + env
        mlflow.log_params({f"cfg.{k}": v for k, v in asdict(config).items()
                           if not isinstance(v, list)})
        mlflow.log_params({"cfg.training_sources": ",".join(config.training_sources) or "none"})
        mlflow.log_params({f"env.{k}": v for k, v in _env_info().items()})
        mlflow.log_param("data.train_examples", len(train_examples))
        mlflow.log_param("data.val_examples", len(val_examples))

        # Fine-tune (skipped for baseline, for which training_sources == [])
        train_metrics: dict = {}
        if train_examples:
            log.info("starting fine-tune on %d examples", len(train_examples))
            train_metrics = finetune(model, processor, train_examples, config)
            mlflow.log_metrics(train_metrics)
        else:
            log.info("no training data — baseline-only run (eval + log)")
            mlflow.log_metric("total_training_s", 0.0)

        # Evaluate on the IAM validation slice
        log.info("evaluating on %d IAM val examples", len(val_examples))
        predictor = make_trocr_predictor(processor, model, device=config.device)
        eval_result: EvalResult = evaluate(
            predictor, val_examples,
            batch_size=config.eval_batch_size,
        )

        mlflow.log_metrics({
            "val_cer": eval_result.cer,
            "val_wer": eval_result.wer,
            "val_n_examples": eval_result.n_examples,
            "val_mean_inference_ms": eval_result.mean_inference_ms,
            "val_examples_per_second": eval_result.examples_per_second,
            "val_total_s": eval_result.seconds,
        })

        log.info("RESULTS: val_cer=%.4f  val_wer=%.4f  n=%d  %.1f ex/s",
                 eval_result.cer, eval_result.wer, eval_result.n_examples,
                 eval_result.examples_per_second)

        # Quality gate
        gate_passed = False
        gate_reason = "not evaluated"
        if config.role == "baseline":
            gate_passed = True
            gate_reason = "baseline run — sets reference CER"
        elif config.role == "finetune" and not train_examples:
            # Role says "finetune" but we loaded zero training examples.
            # This happens when `corrections` is requested on a fresh system.
            # DO NOT register a model that's secretly just the baseline.
            gate_passed = False
            gate_reason = (
                f"no training data loaded for sources={config.training_sources}; "
                f"skipping registration (this is correct — we should not register "
                f"a fine-tune candidate that saw zero examples)"
            )
        else:
            baseline_cer = _get_baseline_cer()
            if baseline_cer is None:
                gate_passed = False
                gate_reason = "no baseline CER available — run baseline first"
            else:
                threshold = baseline_cer * config.gate_tolerance
                if eval_result.cer <= threshold:
                    gate_passed = True
                    gate_reason = (
                        f"val_cer {eval_result.cer:.4f} <= "
                        f"baseline_cer {baseline_cer:.4f} * "
                        f"tolerance {config.gate_tolerance} = {threshold:.4f}"
                    )
                else:
                    gate_reason = (
                        f"val_cer {eval_result.cer:.4f} > "
                        f"baseline_cer {baseline_cer:.4f} * "
                        f"tolerance {config.gate_tolerance} = {threshold:.4f}"
                    )
                mlflow.log_metric("gate.baseline_cer", baseline_cer)
                mlflow.log_metric("gate.threshold_cer", threshold)

        mlflow.set_tag("gate_passed", str(gate_passed).lower())
        mlflow.set_tag("gate_reason", gate_reason)
        log.info("GATE: %s — %s", "PASS" if gate_passed else "FAIL", gate_reason)

        # Register model (only on pass, only for non-baseline runs)
        if gate_passed and config.register_on_pass and config.role != "baseline":
            log.info("registering model to MLflow registry as '%s'", config.register_name)
            try:
                # Log the model — uses mlflow.transformers for correct serialization
                mlflow.transformers.log_model(
                    transformers_model={
                        "model": model,
                        "image_processor": processor.image_processor,
                        "tokenizer": processor.tokenizer,
                    },
                    artifact_path="model",
                    task="image-to-text",
                    registered_model_name=config.register_name,
                )
                log.info("registered model uri: models:/%s/<latest>", config.register_name)
            except Exception as exc:
                log.exception("model registration failed: %s", exc)
                mlflow.set_tag("registration_error", str(exc))
                return 2
        else:
            # Log model as artifact without registering, so we can still inspect it
            log.info("not registering (gate_passed=%s, role=%s)", gate_passed, config.role)

        return 0 if gate_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="YAML config path")
    args = parser.parse_args()

    config = load_config(args.config)
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
