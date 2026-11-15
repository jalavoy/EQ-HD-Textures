"""Batched super-resolution inference with a Spandrel-loadable model."""

from __future__ import annotations

import typing
from pathlib import Path

import numpy as np
import torch
from spandrel import ImageModelDescriptor, ModelLoader


class Upscaler:
    """Wraps an image-to-image model. `scale` is the model's fixed upscale factor."""

    def __init__(self, model_path: Path, device: str = "cuda"):
        # weights_only=True refuses arbitrary pickled objects, so a .pth can't run
        # code. typing.OrderedDict is just a dict type some checkpoints were saved with.
        with torch.serialization.safe_globals([typing.OrderedDict]):
            state = torch.load(model_path, map_location="cpu", weights_only=True)
        model = ModelLoader().load_from_state_dict(state)
        if not isinstance(model, ImageModelDescriptor):
            raise TypeError(f"{model_path}: not an image-to-image model")
        model.to(device).eval()
        if model.supports_half:
            model.half()
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        self.model = model
        self.device = device
        self.scale = model.scale
        self.name = f"{model.architecture.name} x{model.scale}"

    @torch.inference_mode()
    def upscale_many(self, images: list[np.ndarray], pad_mode: str = "wrap",
                     tile: int = 512, budget: int = 4 * 320 * 320,
                     on_progress: typing.Callable[[int, int], None] | None = None) -> list[np.ndarray]:
        """Upscale RGB uint8 images, batching equal-size tiles through the model.

        Each image is padded first ("wrap" keeps tiling textures seamless; use
        "reflect" for art that isn't meant to repeat). Images larger than `tile`
        are split into tiles that each carry `pad` px of context, cropped off
        afterwards so joins don't show. Tiles of the same padded shape are
        stacked into batches of up to `budget` input pixels, which keeps the GPU
        busy on small textures. Models without fp16 support run under bf16 autocast.
        on_progress(pixels, images), if given, is called after each batch with the source
        pixels it covered and the number of images that are now complete.
        """
        s = self.scale
        outs = [np.empty((im.shape[0] * s, im.shape[1] * s, 3), np.uint8) for im in images]
        padded, groups, tiles_left = [], {}, [0] * len(images)
        for i, im in enumerate(images):
            h, w, _ = im.shape
            pad = min(32, max(8, min(h, w) // 4))
            padded.append(torch.from_numpy(np.pad(im, ((pad, pad), (pad, pad), (0, 0)), mode=pad_mode)).permute(2, 0, 1))
            for y in range(0, h, tile):
                for x in range(0, w, tile):
                    th, tw = min(tile, h - y), min(tile, w - x)
                    groups.setdefault((th + 2 * pad, tw + 2 * pad), []).append((i, y, x, th, tw, pad))
                    tiles_left[i] += 1

        weight_dtype = next(self.model.model.parameters()).dtype
        autocast = weight_dtype == torch.float32
        for (ph, pw), jobs in groups.items():
            n = max(1, budget // (ph * pw))
            for k in range(0, len(jobs), n):
                chunk = jobs[k:k + n]
                batch = torch.stack([padded[i][:, y:y + th + 2 * pad, x:x + tw + 2 * pad]
                                     for i, y, x, th, tw, pad in chunk]).to(self.device).to(weight_dtype) / 255.0
                with torch.autocast(self.device, dtype=torch.bfloat16, enabled=autocast):
                    res = self.model(batch)
                res = (res.float().clamp(0, 1) * 255.0).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
                for r, (i, y, x, th, tw, pad) in zip(res, chunk):
                    outs[i][y * s:(y + th) * s, x * s:(x + tw) * s] = r[pad * s:(pad + th) * s, pad * s:(pad + tw) * s]
                finished = 0
                for i, *_ in chunk:
                    tiles_left[i] -= 1
                    finished += tiles_left[i] == 0
                if on_progress:
                    on_progress(sum(th * tw for _, _, _, th, tw, _ in chunk), finished)
        return outs
