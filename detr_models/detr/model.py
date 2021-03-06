import time

# IPDB can be used for debugging.
# Ignoring flake8 error code F401
import ipdb  # noqa: F401
import numpy as np
import tensorflow as tf
from detr_models.backbone.backbone import Backbone
from detr_models.detr.data_feeder import DataFeeder
from detr_models.detr.losses import bbox_loss, score_loss
from detr_models.detr.matcher import bipartite_matching
from detr_models.detr.utils import save_training_loss
from detr_models.detr.uuid_iterator import UUIDIterator
from detr_models.transformer.transformer import Transformer
from tensorflow.keras import Model

tf.keras.backend.set_floatx("float32")


class DETR:
    def __init__(
        self,
        storage_path,
        input_shape,
        num_queries,
        num_classes,
        num_heads,
        dim_transformer,
        dim_feedforward,
        num_transformer_layer,
        backbone_name,
        backbone_config,
        train_backbone=False,
    ):
        """Initialize Detection Transformer (DETR) network.

        Parameters
        ----------
        storage_path : str
            Path to images.
        input_shape : tuple
            Specification of model input [H, W, C].
        num_queries : int
            Number of queries used in transformer.
        num_classes : int
            Number of target classes.
        num_heads : int
            Number of heads in multi-head attention layers.
        dim_transformer : int
            Number of neurons in multi-head attention layers.
            Should be a multiple of `num_heads`.
        dim_feedforward : int
            Number of neurons in transformer feed forward layers.
        num_transformer_layer : int
            Number of layers in transformer network.
        backbone_name : str
            Name of backbone used for DETR network.
        backbone_config : dict
            Config of backbone used for DETR network.
        train_backbone : bool, optional
            Flag to indicate training/inference mode.
        """

        # Save object parameters
        self.storage_path = storage_path
        self.input_shape = input_shape
        self.num_queries = num_queries
        self.num_classes = num_classes
        self.num_heads = num_heads
        self.dim_transformer = dim_transformer
        self.dim_feedforward = dim_feedforward
        self.num_transformer_layer = num_transformer_layer
        self.train_backbone = train_backbone

        # Init Backbone
        self.backbone = Backbone(backbone_name, backbone_config).model
        self.backbone.trainable = train_backbone
        self.fm_shape = self.backbone.get_layer("feature_map").output.shape[1::]
        self.positional_encodings_shape = (
            self.fm_shape[0] * self.fm_shape[1],
            dim_transformer,
        )

        # Init Feeder and Iterator
        self.uuiditerator = UUIDIterator(storage_path)
        self.feeder = DataFeeder(
            storage_path, num_queries, num_classes, self.fm_shape, dim_transformer
        )

    def build_model(self):
        """Build Detection Transformer (DETR) model.

        Returns
        -------
        tf.Model
            Detection Transformer (DETR) model
        """
        batch_input = tf.keras.layers.Input(shape=self.input_shape, name="Batch_Input")
        positional_encodings = tf.keras.layers.Input(
            shape=self.positional_encodings_shape, name="Positional_Encodings_Input"
        )
        feature_map = self.backbone(batch_input)

        transformer_input = tf.keras.layers.Conv2D(self.dim_transformer, kernel_size=1)(
            feature_map
        )

        batch_size = tf.shape(transformer_input)[0]

        transformer_input = tf.reshape(
            transformer_input,
            shape=(
                batch_size,
                transformer_input.shape[1] * transformer_input.shape[2],
                transformer_input.shape[3],
            ),
        )

        # Create Queries
        # Query Input is always a tensor of ones, therefore the output
        # equals the weights of the Embedding Layer
        query_pos = tf.ones((self.num_queries), dtype=tf.float32)
        query_pos = tf.repeat(
            tf.expand_dims(query_pos, axis=0), repeats=batch_size, axis=0
        )
        query_embedding = tf.keras.layers.Embedding(
            input_dim=self.num_queries, output_dim=self.dim_transformer
        )(query_pos)

        transformer = Transformer(
            self.num_transformer_layer,
            self.dim_transformer,
            self.num_heads,
            self.dim_feedforward,
        )

        transformer_output = transformer(
            inp=transformer_input,
            positional_encodings=positional_encodings,
            query_pos=query_embedding,
        )

        cls_pred = tf.keras.layers.Dense(
            units=self.num_classes + 1, activation="softmax"
        )(transformer_output)
        bbox_pred = tf.keras.layers.Dense(units=4, activation="sigmoid")(
            transformer_output
        )

        output_tensor = [cls_pred, bbox_pred]

        return Model([batch_input, positional_encodings], output_tensor, name="DETR")

    def train(
        self,
        epochs,
        optimizer,
        batch_size,
        count_images,
        output_dir,
        use_pretrained=None,
    ):
        """Train the DETR Model.

        Parameters
        ----------
        epochs : int
            Number of training epochs.
        optimizer : tf.Optimizer
            Any chosen optimizer used for training.
        batch_size : int
            Number of samples per batch.
        count_images : int
            Number of total images used for training.
        output_dir: str
            Path used to save the final model weights and training loss.
        use_pretrained : str, optional
            Path to saved pre-trained model weights. Only used if specified and only
            valid if the weights align with the chosen model config.

        Returns
        -------
        float
            Final training loss.
        """

        print("-------------------------------------------", flush=True)
        print("-------------------------------------------\n", flush=True)
        print("Build Model")

        model = self.build_model()

        if use_pretrained:
            print("Used pre-trained model weights\n", flush=True)
            model.load_weights(use_pretrained)

        print("-------------------------------------------\n", flush=True)
        print(f"Start Training - Total of {epochs} Epochs:\n", flush=True)

        detr_loss = []

        for epoch in range(epochs):
            start = time.time()
            print("-------------------------------------------", flush=True)
            print(f"Beginning of Epoch: {epoch+1}\n", flush=True)

            epoch_loss = np.array([0.0, 0.0, 0.0])
            batch_iteration = 0

            # Iterate over all batches
            for batch_uuids in self.uuiditerator(batch_size):
                print(
                    "Batch: {}/{} ".format(
                        batch_iteration + 1, count_images // batch_size
                    ),
                    flush=True,
                )

                (
                    batch_inputs,
                    batch_cls,
                    batch_bbox,
                    obj_indices,
                    positional_encodings,
                ) = self.feeder(batch_uuids)

                batch_loss = _train(
                    detr=model,
                    optimizer=optimizer,
                    batch_inputs=batch_inputs,
                    batch_cls=batch_cls,
                    batch_bbox=batch_bbox,
                    obj_indices=obj_indices,
                    positional_encodings=positional_encodings,
                )

                batch_loss = [loss.numpy() for loss in batch_loss]
                epoch_loss += (1 / len(batch_uuids)) * np.array(batch_loss)
                batch_iteration += 1

            detr_loss.append(epoch_loss)

            print("DETR Loss: %f" % epoch_loss[0], flush=True)
            print(f"Time for epoch {epoch + 1} is {time.time()-start} sec", flush=True)
            print("-------------------------------------------\n", flush=True)

        print("Finalize Training\n", flush=True)
        print("-------------------------------------------\n", flush=True)
        print("Done", flush=True)

        # Save training loss and model
        model.save_weights("{}/detr_weights".format(output_dir))
        save_training_loss(detr_loss, "{}/detr_loss.txt".format(output_dir))

        return detr_loss


@tf.function
def _train(
    detr,
    optimizer,
    batch_inputs,
    batch_cls,
    batch_bbox,
    obj_indices,
    positional_encodings,
):
    """Train step of the DETR network.

    Parameters
    ----------
    detr : tf.Model
        Detection Transformer (DETR) Model.
    optimizer : tf.Optimizer
        Any chosen optimizer used for training.
    batch_inputs : tf.Tensor
        Batch input images of shape [Batch Size, H, W, C].
    batch_cls : tf.Tensor
        Batch class targets of shape [Batch Size, #Queries, 1].
    batch_bbox : tf.Tensor
        Batch bounding box targets of shape [Batch Size, #Queries, 4].
    obj_indices : tf.RaggedTensor
        Helper tensor of shape [Batch Size, None].
        Used to link objects in the cost matrix to the target tensors.
    positional_encodings : tf.Tensor
        Positional encodings of shape [Batch Size, H*W, dim_transformer].
        Used in transformer network to enrich input information.
    """
    num_objects = tf.cast(tf.math.reduce_max(obj_indices), dtype=tf.float32)

    if tf.math.equal(num_objects, 0):
        num_objects = tf.constant(1.0, dtype=tf.float32)

    with tf.GradientTape() as gradient_tape:
        detr_scores, detr_bbox = detr(
            [batch_inputs, positional_encodings], training=True
        )

        indices = bipartite_matching(
            detr_scores, detr_bbox, batch_cls, batch_bbox, obj_indices
        )

        score_loss = calculate_score_loss(batch_cls, detr_scores, indices)

        bbox_loss = calculate_bbox_loss(batch_bbox, detr_bbox, indices)
        bbox_loss = bbox_loss / num_objects

        detr_loss = score_loss + bbox_loss

        gradients = gradient_tape.gradient(detr_loss, detr.trainable_variables)
        optimizer.apply_gradients(zip(gradients, detr.trainable_variables))

    return [detr_loss, score_loss, bbox_loss]


def calculate_score_loss(batch_cls, detr_scores, indices):
    """Helper function to calculate the score loss.

    Parameters
    ----------
    batch_cls : tf.Tensor
        Batch class targets of shape [Batch Size, #Queries, 1].
    detr_scores : tf.Tensor
        Batch detr score outputs of shape [Batch Size. #Queries, #Classes + 1].
    indices : tf.Tensor
        Bipartite matching indices of shape [Batch Size, 2, max_obj].
        Indicating the assignement between queries and objects in each sample. Note that `max_obj` is
        specified in `tf_linear_sum_assignment` and the tensor is padded with `-1`.

    Returns
    -------
    tf.Tensor
        Batch score loss.
    """
    batch_score_loss = tf.map_fn(
        lambda el: score_loss(*el),
        elems=[batch_cls, detr_scores, indices],
        dtype=tf.float32,
    )
    return tf.reduce_sum(batch_score_loss)


def calculate_bbox_loss(batch_bbox, detr_bbox, indices):
    """Helper function to calculate the bounding box loss.

    Parameters
    ----------
    batch_bbox : tf.Tensor
        Batch bounding box targets of shape [Batch Size, #Queries, 4].
    detr_bbox : tf.Tensor
        Batch detr bounding box outputs of shape [Batch Size. #Queries, 4].
    indices : tf.Tensor
        Bipartite matching indices of shape [Batch Size, 2, max_obj].
        Indicating the assignement between queries and objects in each sample. Note that `max_obj` is
        specified in `tf_linear_sum_assignment` and the tensor is padded with `-1`.

    Returns
    -------
    tf.Tensor
        Batch bounding box loss
    """

    batch_bbox_loss = tf.map_fn(
        lambda el: bbox_loss(*el),
        elems=[batch_bbox, detr_bbox, indices],
        dtype=tf.float32,
    )

    return tf.reduce_sum(batch_bbox_loss)
