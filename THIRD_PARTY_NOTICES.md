# Third party notices

Pet Recognition is released under the MIT license. The app, Pet Recognition
Engine, downloads and runs the following models when its image is built.
They are not part of this repository.

| Model | Source | License |
|---|---|---|
| YOLOX-S | `yolox_s.onnx` from release 0.1.1rc0 of [Megvii-BaseDetection/YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) | Apache-2.0 |
| DINOv2 small | ONNX export [onnx-community/dinov2-small-ONNX](https://huggingface.co/onnx-community/dinov2-small-ONNX) of the weights of [facebookresearch/dinov2](https://github.com/facebookresearch/dinov2) | Apache-2.0 |

The app also contains a sample picture, `pet_recognition_engine/selftest/cat.jpg`,
used to test the models when the image is built. It is "Chelsea the cat" by
Stéfan van der Walt, from the sample data of
scikit-image, released under CC0 1.0.

The app's Python dependencies, and their licenses, are listed in
`pet_recognition_engine/requirements.txt`; CI checks that each license is on
the allowed list.
