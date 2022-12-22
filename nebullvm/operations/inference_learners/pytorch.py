import os
import pickle
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Tuple, Union, Optional, List

from nebullvm.operations.inference_learners.base import (
    PytorchBaseInferenceLearner,
    LearnerMetadata,
)
from nebullvm.optional_modules.torch import (
    torch,
    symbolic_trace,
    Module,
    ScriptModule,
    GraphModule,
)
from nebullvm.tools.base import Device, ModelParams
from nebullvm.tools.transformations import MultiStageTransformation


class PytorchBackendInferenceLearner(PytorchBaseInferenceLearner):
    MODEL_NAME = "model_scripted.pt"
    name = "TorchScript"

    def __init__(self, torch_model: ScriptModule, device: Device, **kwargs):
        super().__init__(**kwargs)
        self.model = torch_model.eval()
        if device is Device.GPU:
            self.model.cuda()
        self.device = device

    def run(self, *input_tensors: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        device = input_tensors[0].device
        if self.device is Device.GPU:
            input_tensors = (t.cuda() for t in input_tensors)
        with torch.no_grad():
            res = self.model(*input_tensors)
            if not isinstance(res, tuple):
                res = res.to(device)
                return (res,)
            return tuple(out.to(device) for out in res)

    def get_size(self):
        try:
            if hasattr(self.model, "core_model"):
                return len(pickle.dumps(self.model.core_model, -1))
            else:
                # Normal torch model
                return len(pickle.dumps(self.model, -1))
        except RuntimeError:
            with TemporaryDirectory() as tmp_dir:
                self.save(tmp_dir)
                return sum(
                    os.path.getsize(Path(tmp_dir) / f)
                    for f in os.listdir(Path(tmp_dir))
                    if os.path.isfile(Path(tmp_dir) / f)
                )

    def save(self, path: Union[str, Path], **kwargs):
        path = Path(path)
        path.mkdir(exist_ok=True)
        metadata = LearnerMetadata.from_model(self, **kwargs)
        metadata.save(path)

        torch.jit.save(self.model, path / self.MODEL_NAME)

    @classmethod
    def load(cls, path: Union[Path, str], **kwargs):
        path = Path(path)
        model = torch.jit.load(path / cls.MODEL_NAME)
        metadata = LearnerMetadata.read(path)
        return cls(
            torch_model=model,
            network_parameters=ModelParams(**metadata.network_parameters),
            input_tfms=MultiStageTransformation.from_dict(metadata.input_tfms)
            if metadata.input_tfms is not None
            else None,
            device=metadata.device,
        )

    @classmethod
    def from_torch_model(
        cls,
        model: Union[Module, GraphModule],
        network_parameters: ModelParams,
        device: Device,
        input_tfms: Optional[MultiStageTransformation] = None,
        input_data: List[torch.Tensor] = None,
    ):
        if device is Device.GPU:
            input_data = [t.cuda() for t in input_data]

        if not isinstance(model, torch.fx.GraphModule):
            model.eval()
            try:
                model_scripted = symbolic_trace(model)
                model_scripted = torch.jit.script(model_scripted)
            except Exception:
                try:
                    model_scripted = torch.jit.script(model)
                except Exception:
                    model_scripted = torch.jit.trace(model, tuple(input_data))
        else:
            model_scripted = torch.jit.script(model)

        return cls(
            torch_model=model_scripted,
            network_parameters=network_parameters,
            input_tfms=input_tfms,
            input_data=input_data,
            device=device,
        )
