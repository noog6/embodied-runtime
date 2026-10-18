"""Deployment-owned construction of camera interpretation backends."""

from embodied_runtime.config import (
    ConfigurationError, VisionConfiguration, validate_vision_configuration,
)
from .visual import (
    OpenAICompatibleVisualPerceptionBackend, OpenAIResponsesVisualPerceptionBackend,
    OrderedVisualPerceptionBackend,
)


def create_visual_perception_backend(selection: str, config: VisionConfiguration = VisionConfiguration(),
                                     *, clients=None):
    validate_vision_configuration(config)
    if selection == "none":
        return None
    if selection == "openai-responses":
        if config.backends:
            raise ConfigurationError("openai-responses cannot discard vision.backends; select configured or none")
        backend = OpenAIResponsesVisualPerceptionBackend(request_timeout_seconds=config.request_timeout_seconds,
                                                       client=clients[0] if clients else None)
        backend.image_limits = config.image_limits
        return backend
    if selection != "configured" or not config.backends:
        raise ConfigurationError("configured vision requires vision.backends")
    if clients is not None and len(clients) != len(config.backends):
        raise ValueError("one injected client is required per vision backend")
    backends = []
    for index, entry in enumerate(config.backends):
        client = clients[index] if clients is not None else None
        if entry.backend == "openai-compatible":
            backend = OpenAICompatibleVisualPerceptionBackend(config=entry, client=client)
        else:
            backend = OpenAIResponsesVisualPerceptionBackend(model=entry.model, api_key_env=entry.api_key_env,
                        request_timeout_seconds=entry.request_timeout_seconds, client=client)
        backend.image_limits = config.image_limits
        backends.append(backend)
    ordered = OrderedVisualPerceptionBackend(backends, request_timeout_seconds=config.request_timeout_seconds)
    ordered.image_limits = config.image_limits
    return ordered
