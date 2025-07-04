# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements Feature Projection Distillation Strategy."""

from typing import Callable
from flax import nnx
import jax
import jax.numpy as jnp
import optax
from tunix.distillation import feature_extraction
from tunix.distillation.strategies import base_strategy
from typing_extensions import override

ModelForwardCallable = base_strategy.ModelForwardCallable


class FeatureProjectionStrategy(base_strategy.BaseStrategy):
  """Implements Feature Projection distillation.

  This strategy captures the feature maps and computes loss (typically mse)
  between projected student and teacher feature maps from selected feature
  layers. It combines this feature mse loss with a standard task loss (softmax
  cross-entropy) on the student's final logits.
  """

  def __init__(
      self,
      student_forward_fn: ModelForwardCallable[jax.Array],
      teacher_forward_fn: ModelForwardCallable[jax.Array],
      labels_fn: Callable[..., jax.Array],
      feature_layer: type[nnx.Module],
      dummy_input: dict[str, jax.Array],
      rngs: nnx.Rngs,
      alpha: float = 0.75,
      feature_loss_fn: (
          Callable[[jax.Array, jax.Array], jax.Array] | None
      ) = None,
  ):
    """Initializes the FeatureProjection strategy.

    Args:
        student_forward_fn: Function to compute student model outputs.
        teacher_forward_fn: Function to compute teacher model outputs.
        labels_fn: Function to compute labels from model inputs.
        feature_layer: The feature layer to use for distillation.
        dummy_input: A dictionary of dummy inputs to perform a forward pass on
          the models.
        rngs: A random number generator.
        alpha: Weight to balance feature loss and task loss (0.0 to 1.0).
        feature_loss_fn: A function that takes two jax. Arrays (student_map,
          teacher_map) and returns a scalar loss. Defaults to MSE.
    """
    super().__init__(student_forward_fn, teacher_forward_fn, labels_fn)
    if not (0.0 <= alpha <= 1.0):
      raise ValueError(
          f"Alpha must be a float between 0.0 and 1.0, got {alpha}"
      )

    self.alpha = alpha
    self.feature_loss_fn = feature_loss_fn
    if feature_loss_fn is None:
      self.feature_loss_fn = (
          lambda student_features, teacher_features: jnp.mean(
              jnp.square(student_features - teacher_features)
          )
      )
    self.feature_layer = feature_layer
    self.dummy_input = dummy_input
    self.rngs = rngs

  @override
  def pre_process_models(
      self,
      student_model: nnx.Module,
      teacher_model: nnx.Module,
  ) -> tuple[nnx.Module, nnx.Module]:
    """Pre-processes the models for distillation."""
    return feature_extraction.setup_models_with_feature_projection(
        student_model,
        teacher_model,
        self.feature_layer,
        self.feature_layer,
        self.dummy_input,
        self.dummy_input,
        rngs=self.rngs,
    )

  @override
  def post_process_models(
      self,
      student_model: nnx.Module,
      teacher_model: nnx.Module,
  ) -> tuple[nnx.Module, nnx.Module]:
    """Post-processes the models after distillation."""
    return feature_extraction.remove_feature_projection_from_models(
        student_model, teacher_model
    )

  @override
  def get_teacher_outputs(
      self,
      teacher_model: nnx.Module,
      inputs: dict[str, jax.Array],
  ) -> jax.Array:
    """Computes the teacher model outputs."""
    _ = self._teacher_forward_fn(teacher_model, **inputs)
    teacher_features = feature_extraction.pop_sowed_intermediate_outputs(
        teacher_model
    )
    return jnp.stack(jax.tree.leaves(teacher_features))

  @override
  def get_student_outputs(
      self,
      student_model: nnx.Module,
      inputs: dict[str, jax.Array],
  ) -> dict[str, jax.Array]:
    """Computes the student model outputs."""
    student_logits, student_features = self._student_forward_fn(
        student_model, **inputs
    )
    return {"logits": student_logits, "features": student_features}

  def compute_eval_loss(
      self,
      student_output: dict[str, jax.Array],
      labels: jax.Array,
  ) -> jax.Array:
    """Computes the task loss.

    Args:
        student_output: Dictionary from student model, must contain 'logits'.
        labels: The ground truth labels for the examples.

    Returns:
        A JAX array representing the task loss, averaged over the batch.
    """
    # Calculate Task Loss (Cross-Entropy on original student logits)
    ce_loss = optax.softmax_cross_entropy(
        logits=student_output["logits"], labels=labels
    )
    task_loss = jnp.mean(ce_loss)

    return task_loss

  def compute_loss(
      self,
      student_output: dict[str, jax.Array],
      teacher_output: jax.Array,
      labels: jax.Array,
  ) -> jax.Array:
    """Computes the combined feature projection and task loss.

    Args:
        student_output: Dictionary from student model, must contain 'logits' and
          'features'.
        teacher_output: Feature map from teacher model.
        labels: The ground truth labels (e.g., one-hot encoded).

    Returns:
        A JAX array representing the combined distillation and task loss,
        averaged over the batch.
    """
    if "logits" not in student_output or "features" not in student_output:
      raise TypeError(
          "student_outputs must be a dict containing 'logits' and 'features'"
      )

    student_logits = student_output["logits"]
    student_features = student_output["features"]

    feature_loss = self.feature_loss_fn(student_features, teacher_output)

    # Calculate Task Loss (Cross-Entropy)
    ce_loss_per_example = optax.softmax_cross_entropy(
        logits=student_logits, labels=labels
    )
    task_loss = jnp.mean(ce_loss_per_example)

    # Combine the losses
    combined_loss = (self.alpha * feature_loss) + (
        (1.0 - self.alpha) * task_loss
    )

    return combined_loss
