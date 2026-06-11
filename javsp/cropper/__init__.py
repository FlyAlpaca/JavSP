from javsp.config import SlimefaceEngine
from javsp.cropper.interface import DefaultCropper
from javsp.cropper.slimeface_crop import SlimefaceCropper


def get_cropper(engine: SlimefaceEngine | None) -> DefaultCropper | SlimefaceCropper:
    if engine is None:
        return DefaultCropper()
    if engine.name == "slimeface":
        return SlimefaceCropper()
