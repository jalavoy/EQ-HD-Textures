"""Upscale EverQuest .s3d textures with a local super-resolution model."""

__version__ = "0.1.0"

# Bump when processing changes in a way that changes output bytes, so archives
# processed by an older version are re-rendered on the next run.
PIPELINE_VERSION = "2"
