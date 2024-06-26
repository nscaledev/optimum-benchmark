import gc
import os
from collections import OrderedDict
from logging import getLogger
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

import torch
from hydra.utils import get_class
from onnxruntime import SessionOptions
from optimum.onnxruntime import ONNX_DECODER_NAME, ONNX_DECODER_WITH_PAST_NAME, ORTOptimizer, ORTQuantizer
from optimum.onnxruntime.configuration import (
    AutoCalibrationConfig,
    AutoOptimizationConfig,
    AutoQuantizationConfig,
    CalibrationConfig,
    OptimizationConfig,
    QuantizationConfig,
)
from safetensors.torch import save_file

from ...generators.dataset_generator import DatasetGenerator
from ...task_utils import TEXT_GENERATION_TASKS
from ..base import Backend
from ..transformers_utils import random_init_weights
from .config import ORTConfig
from .utils import TASKS_TO_ORTMODELS, TASKS_TO_ORTSD, format_calibration_config, format_quantization_config

LOGGER = getLogger("onnxruntime")

PROBLEMATIC_INPUTS = ["token_type_ids", "position_ids"]


class ORTBackend(Backend[ORTConfig]):
    NAME: str = "onnxruntime"

    def __init__(self, config: ORTConfig) -> None:
        super().__init__(config)
        self.validate_task()

        self.session_options = SessionOptions()
        if self.config.session_options:
            LOGGER.info("\t+ Processing session options")
            for key, value in self.config.session_options.items():
                setattr(self.session_options, key, value)

        LOGGER.info("\t+ Creating backend temporary directory")
        self.tmpdir = TemporaryDirectory()

        if self.config.no_weights:
            LOGGER.info("\t+ Loading no weights ORTModel")
            self.load_ortmodel_with_no_weights()
        else:
            LOGGER.info("\t+ Loading pretrained ORTModel")
            self.load_ortmodel_from_pretrained()

        if self.is_optimized or self.is_quantized:
            original_model, self.config.model = self.config.model, self.pretrained_model.model_save_dir

        if self.is_optimized:
            LOGGER.info("\t+ Applying ORT optimization")
            self.optimize_onnx_files()
            self.config.model = self.optimized_model

        if self.is_quantized:
            LOGGER.info("\t+ Applying ORT quantization")
            self.quantize_onnx_files()
            self.config.model = self.quantized_model

        if self.is_optimized or self.is_quantized:
            original_export, self.config.export = self.config.export, False
            LOGGER.info("\t+ Loading optimized/quantized ORTModel")
            self.load_ortmodel_from_pretrained()
            self.config.model, self.config.export = original_model, original_export

        self.validate_provider()
        self.tmpdir.cleanup()

    def validate_task(self) -> None:
        if self.config.task in TASKS_TO_ORTSD:
            self.ortmodel_class = get_class(TASKS_TO_ORTSD[self.config.task])
            LOGGER.info(f"Using ORTStableDiffusion class {self.ortmodel_class.__name__}")
        elif self.config.task in TASKS_TO_ORTMODELS:
            self.ortmodel_class = get_class(TASKS_TO_ORTMODELS[self.config.task])
            LOGGER.info(f"Using ORTModel class {self.ortmodel_class.__name__}")
        else:
            raise NotImplementedError(f"ORTBackend does not support task {self.config.task}")

    def validate_provider(self) -> None:
        if not self.pretrained_model.providers[0] == self.config.provider:
            raise ValueError(
                f"{self.config.provider} is not first in providers list: {self.pretrained_model.providers}"
            )

    def create_no_weights_model(self) -> None:
        self.no_weights_model = os.path.join(self.tmpdir.name, "no_weights_model")
        LOGGER.info("\t+ Creating no weights model directory")
        os.makedirs(self.no_weights_model, exist_ok=True)
        LOGGER.info("\t+ Creating no weights model state dict")
        state_dict = torch.nn.Linear(1, 1).state_dict()
        LOGGER.info("\t+ Saving no weights model safetensors")
        safetensors = os.path.join(self.no_weights_model, "model.safetensors")
        save_file(tensors=state_dict, filename=safetensors, metadata={"format": "pt"})

        if self.config.library == "transformers":
            LOGGER.info("\t+ Saving no weights model pretrained config")
            self.pretrained_config.save_pretrained(save_directory=self.no_weights_model)

    def load_ortmodel_with_no_weights(self) -> None:
        LOGGER.info("\t+ Creating no weights model")
        self.create_no_weights_model()

        with random_init_weights():
            original_model, self.config.model = self.config.model, self.no_weights_model
            LOGGER.info("\t+ Loading no weights ORTModel")
            self.load_ortmodel_from_pretrained()
            self.config.model = original_model

    def load_ortmodel_from_pretrained(self) -> None:
        self.pretrained_model = self.ortmodel_class.from_pretrained(
            self.config.model,
            export=self.config.export,
            session_options=self.session_options,
            provider_options=self.config.provider_options,
            use_io_binding=self.config.use_io_binding,
            provider=self.config.provider,
            **self.config.hub_kwargs,
            **self.ortmodel_kwargs,
        )

    @property
    def is_optimized(self) -> bool:
        return (self.config.auto_optimization is not None) or self.config.optimization

    @property
    def is_quantized(self) -> bool:
        return (self.config.auto_quantization is not None) or self.config.quantization

    @property
    def is_calibrated(self) -> bool:
        return (self.config.auto_calibration is not None) or self.config.calibration

    @property
    def ortmodel_kwargs(self) -> Dict[str, Any]:
        kwargs = {}

        if self.config.task in TEXT_GENERATION_TASKS:
            kwargs["use_cache"] = self.config.use_cache
            kwargs["use_merged"] = self.config.use_merged

        return kwargs

    @property
    def onnx_files_names(self):
        assert os.path.isdir(self.config.model), f"{self.config.model} is not a directory"
        if self.config.use_merged:
            return [
                model
                for model in os.listdir(self.config.model)
                if model not in [ONNX_DECODER_NAME, ONNX_DECODER_WITH_PAST_NAME] and model.endswith(".onnx")
            ]
        else:
            return [file for file in os.listdir(self.config.model) if file.endswith(".onnx")]

    @property
    def inputs_names(self) -> List[str]:
        if hasattr(self.pretrained_model, "inputs_names"):
            return self.pretrained_model.inputs_names
        elif hasattr(self.pretrained_model, "input_names"):
            return self.pretrained_model.input_names
        else:
            return []

    def optimize_onnx_files(self) -> None:
        LOGGER.info("\t+ Attempting optimization")
        self.optimized_model = os.path.join(self.tmpdir.name, "optimized")
        LOGGER.info("\t+ Processing optimization config")
        if self.config.auto_optimization is not None:
            optimization_config = AutoOptimizationConfig.with_optimization_level(
                optimization_level=self.config.auto_optimization,
                for_gpu=(self.config.device == "cuda"),
                **self.config.auto_optimization_config,
            )
        elif self.config.optimization:
            optimization_config = OptimizationConfig(
                optimize_for_gpu=(self.config.device == "cuda"), **self.config.optimization_config
            )
        LOGGER.info("\t+ Creating optimizer")
        optimizer = ORTOptimizer.from_pretrained(self.config.model, file_names=self.onnx_files_names)
        LOGGER.info("\t+ Optimizing ORTModel")
        optimizer.optimize(
            optimization_config,
            save_dir=self.optimized_model,
            # TODO: add support for these
            use_external_data_format=None,
            one_external_file=True,
            file_suffix="",
        )
        if self.pretrained_processor is not None:
            self.pretrained_processor.save_pretrained(self.optimized_model)
        if self.pretrained_config is not None:
            self.pretrained_config.save_pretrained(self.optimized_model)

    def quantize_onnx_files(self) -> None:
        LOGGER.info("\t+ Attempting quantization")
        self.quantized_model = f"{self.tmpdir.name}/quantized_model"

        if self.is_calibrated and len(self.onnx_files_names) > 1:
            raise NotImplementedError(
                "Calibrated/Static Quantization is not supported for models with multiple components. "
                f"Found {len(self.onnx_files_names)} components."
            )

        LOGGER.info("\t+ Processing quantization config")
        if self.config.auto_quantization is not None:
            auto_quantization_config = format_quantization_config(self.config.auto_quantization_config)
            auto_quantization_class = getattr(AutoQuantizationConfig, self.config.auto_quantization)
            quantization_config = auto_quantization_class(**auto_quantization_config)
        elif self.config.quantization:
            quantization_config = format_quantization_config(self.config.quantization_config)
            quantization_config = QuantizationConfig(**quantization_config)

        if self.is_calibrated:
            LOGGER.info("\t+ Generating calibration dataset")
            dataset_shapes = {"dataset_size": 1, "sequence_length": 1, **self.model_shapes}
            calibration_dataset = DatasetGenerator(
                task=self.config.task, dataset_shapes=dataset_shapes, model_shapes=self.model_shapes
            )()
            columns_to_be_removed = list(set(calibration_dataset.column_names) - set(self.inputs_names))
            calibration_dataset = calibration_dataset.remove_columns(columns_to_be_removed)

            LOGGER.info("\t+ Processing calibration config")
            if self.config.auto_calibration is not None:
                LOGGER.info("\t+ Processing calibration config")
                auto_calibration_method = getattr(AutoCalibrationConfig, self.config.auto_calibration)
                calibration_config = auto_calibration_method(calibration_dataset, **self.config.auto_calibration_config)
            elif self.config.calibration:
                LOGGER.info("\t+ Processing calibration config")
                calibration_config = format_calibration_config(self.config.calibration_config)
                calibration_config = CalibrationConfig(
                    dataset_name="calibration_dataset",
                    dataset_split=calibration_dataset.split,
                    dataset_num_samples=calibration_dataset.num_rows,
                    dataset_config_name=calibration_dataset.config_name,
                    **self.config.calibration_config,
                )

        for onnx_file_name in self.onnx_files_names:
            LOGGER.info(f"\t+ Creating quantizer for {onnx_file_name}")
            quantizer = ORTQuantizer.from_pretrained(self.config.model, file_name=onnx_file_name)

            if self.is_calibrated:
                LOGGER.info("\t+ Fitting calibration tensors range")
                calibration_tensors_range = quantizer.fit(
                    dataset=calibration_dataset,
                    use_gpu=(self.config.device == "cuda"),
                    calibration_config=calibration_config,
                    operators_to_quantize=quantization_config.operators_to_quantize,
                    # TODO: add support for these (maybe)
                    use_external_data_format=False,
                    force_symmetric_range=False,
                    batch_size=1,
                )
            else:
                calibration_tensors_range = None

            LOGGER.info("\t+ Quantizing model")
            quantizer.quantize(
                save_dir=self.quantized_model,
                quantization_config=quantization_config,
                calibration_tensors_range=calibration_tensors_range,
                # TODO: add support for these (maybe)
                use_external_data_format=False,
                preprocessor=None,
                file_suffix="",
            )
        if self.pretrained_processor is not None:
            self.pretrained_processor.save_pretrained(self.quantized_model)
        if self.pretrained_config is not None:
            self.pretrained_config.save_pretrained(self.quantized_model)

    def prepare_inputs(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        inputs = super().prepare_inputs(inputs)

        if self.config.library == "diffusers":
            inputs = {"prompt": inputs["prompt"]}
        else:
            for key, value in list(inputs.items()):
                if key not in PROBLEMATIC_INPUTS:
                    inputs[key] = value.to(self.config.device)
                elif key not in self.inputs_names:
                    inputs.pop(key)

        return inputs

    @torch.inference_mode()
    def forward(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.forward(**inputs, **kwargs)

    @torch.inference_mode()
    def prefill(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.generate(**inputs, **kwargs)

    @torch.inference_mode()
    def generate(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model.generate(**inputs, **kwargs)

    @torch.inference_mode()
    def call(self, inputs: Dict[str, Any], kwargs: Dict[str, Any]) -> OrderedDict:
        return self.pretrained_model(**inputs, **kwargs)

    def clean(self) -> None:
        super().clean()

        if hasattr(self, "tmpdir"):
            LOGGER.info("\t+ Cleaning backend temporary directory")
            self.tmpdir.cleanup()

        gc.collect()
