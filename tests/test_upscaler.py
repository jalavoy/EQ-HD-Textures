"""Small pieces of the real upscaler that don't need a GPU."""

from eq_hd import upscaler


def test_autocast_gets_a_device_type_not_a_device_index():
    # torch.autocast("cuda:1") raises "unsupported autocast device_type"; it wants "cuda".
    assert upscaler.autocast_type("cuda:1") == "cuda"
    assert upscaler.autocast_type("cuda") == "cuda"
    assert upscaler.autocast_type("cpu") == "cpu"
