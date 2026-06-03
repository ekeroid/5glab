"""
Triton Python backend — JPEG preprocessing for YOLOv8.

Decodes JPEG, resizes to 640x640, normalizes to [0,1], converts to NCHW.
Uses OpenCV + NumPy which are available in tritonserver:24.10-py3.
max_batch_size=0, so each request is a single unbatched inference.
"""

import cv2
import numpy as np

import triton_python_backend_utils as pb_utils


class TritonPythonModel:
    def initialize(self, args):
        pass

    def execute(self, requests):
        responses = []
        for request in requests:
            jpeg_tensor = pb_utils.get_input_tensor_by_name(request, "IMAGE_BYTES")
            # With max_batch_size=0, as_numpy() returns shape (N,) directly
            jpeg_buf = jpeg_tensor.as_numpy()

            # Decode JPEG with OpenCV
            img = cv2.imdecode(jpeg_buf, cv2.IMREAD_COLOR)

            if img is None:
                raise pb_utils.TritonModelException(
                    f"cv2.imdecode failed: shape={jpeg_buf.shape}, "
                    f"dtype={jpeg_buf.dtype}, first4={jpeg_buf[:4].tolist()}"
                )

            # Resize to 640x640
            img = cv2.resize(img, (640, 640), interpolation=cv2.INTER_LINEAR)

            # BGR -> RGB, normalize to [0, 1], HWC -> CHW
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = img.astype(np.float32) / 255.0
            img = np.transpose(img, (2, 0, 1))  # (3, 640, 640)

            output_tensor = pb_utils.Tensor("images", img)
            responses.append(pb_utils.InferenceResponse([output_tensor]))

        return responses

    def finalize(self):
        pass
