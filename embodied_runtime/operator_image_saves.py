"""Exact operator commands for one request-scoped durable image write."""

from dataclasses import dataclass
import json
import unicodedata

from embodied_runtime.jobs.workspace import validate_artifact_path

IMAGE_SAVE_COMMAND_PREFIX = "save image "
MAX_IMAGE_SAVE_COMMAND_CHARS = 1024


@dataclass(frozen=True, slots=True)
class OperatorImageSaveRequest:
    job: str
    path: str
    mode: str


@dataclass(frozen=True, slots=True)
class OperatorImageSaveAuthorization:
    """Runtime-resolved grant; never accepted as a model tool argument."""

    episode_id: int
    job_id: int
    path: str
    mode: str

    def permits(self, episode_id: int | None, job_id: int, path: str, mode: str) -> bool:
        return (self.episode_id == episode_id and self.job_id == job_id
                and self.path == path and self.mode == mode)


def parse_image_save_request(message: str) -> OperatorImageSaveRequest | None:
    """Only a complete explicit command grants intent; prose never does."""
    message = message.strip()
    if not message.startswith(IMAGE_SAVE_COMMAND_PREFIX):
        return None
    if len(message) > MAX_IMAGE_SAVE_COMMAND_CHARS:
        raise ValueError("invalid image-save authorization")

    def unique_object(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError("ambiguous image-save authorization")
            record[key] = value
        return record

    record = json.loads(message[len(IMAGE_SAVE_COMMAND_PREFIX):], object_pairs_hook=unique_object)
    if (type(record) is not dict or set(record) != {"job", "path", "mode"}
            or any(type(value) is not str for value in record.values())):
        raise ValueError("invalid image-save authorization")
    job = record["job"].strip()
    if (not job or len(job) > 200
            or any(unicodedata.category(char) == "Cc" for char in job)
            or record["mode"] not in ("create", "replace", "upsert")):
        raise ValueError("invalid image-save authorization")
    validate_artifact_path(record["path"])
    return OperatorImageSaveRequest(job, record["path"], record["mode"])
