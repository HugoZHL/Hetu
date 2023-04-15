# only for DLRMs now

from .base import EmbeddingTrainer
from .hash import HashEmbTrainer
from .compo import CompoEmbTrainer
from .tensortrain import TTEmbTrainer
from .dhe import DHETrainer
from .robe import ROBETrainer
from .dpq import DPQTrainer
from .md import MDETrainer
from .autodim import AutoDimTrainer
from .deeplight import DeepLightTrainer
from .quantize import QuantizeEmbTrainer
from ..layers import Embedding, HashEmbedding, \
    CompositionalEmbedding, TensorTrainEmbedding, \
    DeepHashEmbedding, RobeEmbedding, \
    DPQEmbedding, \
    MDEmbedding, AutoDimEmbedding, \
    DeepLightEmbedding, QuantizedEmbedding


_layer2trainer_mapping = {
    Embedding: EmbeddingTrainer,
    HashEmbedding: HashEmbTrainer,
    CompositionalEmbedding: CompoEmbTrainer,
    TensorTrainEmbedding: TTEmbTrainer,
    DeepHashEmbedding: DHETrainer,
    RobeEmbedding: ROBETrainer,
    DPQEmbedding: DPQTrainer,
    MDEmbedding: MDETrainer,
    AutoDimEmbedding: AutoDimTrainer,
    DeepLightEmbedding: DeepLightTrainer,
    QuantizedEmbedding: QuantizeEmbTrainer,
}

_trainer2layer_mapping = {value: key for key,
                          value in _layer2trainer_mapping.items()}


def get_trainer(layer_type):
    trainer = _layer2trainer_mapping[layer_type]
    return trainer


def get_layer_type(trainer):
    layer_type = _trainer2layer_mapping[trainer]
    return layer_type
