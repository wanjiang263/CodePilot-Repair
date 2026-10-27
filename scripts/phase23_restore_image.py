"""Restore a fixed public OCI image using verified local layers and HTTPS blobs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import tarfile
from pathlib import Path

import httpx

import docker

ROOT = Path(__file__).resolve().parents[1]


class StreamReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.buffer = bytearray()

    def read(self, size=-1):
        while size < 0 or len(self.buffer) < size:
            try:
                self.buffer.extend(next(self.chunks))
            except StopIteration:
                break
        if size < 0:
            size = len(self.buffer)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    return "sha256:" + result.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--reuse-image", required=True)
    parser.add_argument("--skip-reuse-export", action="store_true")
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"[a-z0-9_./-]+", args.repository)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", args.expected_digest)
    output = args.output.resolve()
    assert output.is_relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    cache = ROOT / "artifacts/phase23/image-blobs/sha256"
    cache.mkdir(parents=True, exist_ok=True)
    client = docker.from_env(timeout=60)
    reuse = client.images.get(args.reuse_image)
    reused = []
    if not args.skip_reuse_export:
        chunks = reuse.save(chunk_size=1024**2, named=True)
        try:
            with tarfile.open(fileobj=StreamReader(chunks), mode="r|") as archive:
                for member in archive:
                    name = member.name.removeprefix("./")
                    if not member.isfile() or not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
                        continue
                    target = cache / name.split("/")[-1]
                    if target.exists():
                        assert digest(target) == "sha256:" + target.name
                        continue
                    stream = archive.extractfile(member)
                    assert stream is not None
                    temporary = target.with_suffix(".part")
                    with temporary.open("wb") as writer:
                        shutil.copyfileobj(stream, writer, length=1024**2)
                    assert digest(temporary) == "sha256:" + target.name
                    temporary.rename(target)
                    reused.append(target.name)
                    print("local verified", target.name, member.size, flush=True)
        finally:
            chunks.close()
    required = {}
    manifests = {}
    with httpx.Client(
        proxy=args.proxy,
        trust_env=False,
        timeout=httpx.Timeout(120, connect=20),
        follow_redirects=True,
    ) as http:
        response = http.get(
            "https://auth.docker.io/token",
            params={
                "service": "registry.docker.io",
                "scope": "repository:" + args.repository + ":pull",
            },
        )
        response.raise_for_status()
        headers = {
            "Authorization": "Bearer " + response.json()["token"],
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json",
        }

        def manifest(value):
            response = http.get(
                "https://registry-1.docker.io/v2/" + args.repository + "/manifests/" + value,
                headers=headers,
            )
            response.raise_for_status()
            content = response.content
            assert "sha256:" + hashlib.sha256(content).hexdigest() == value
            target = cache / value.split(":")[1]
            if not target.exists():
                target.write_bytes(content)
            assert digest(target) == value
            document = response.json()
            manifests[value] = {
                "mediaType": document["mediaType"],
                "digest": value,
                "size": len(content),
            }
            required[value] = target
            if "manifests" in document:
                for descriptor in document["manifests"]:
                    manifest(descriptor["digest"])
            else:
                for descriptor in [document["config"], *document["layers"]]:
                    value = descriptor["digest"]
                    assert re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                    target = cache / value.split(":")[1]
                    if not target.exists():
                        assert shutil.disk_usage(ROOT).free > 15 * 1024**3
                        temporary = target.with_suffix(".part")
                        with http.stream(
                            "GET",
                            "https://registry-1.docker.io/v2/"
                            + args.repository
                            + "/blobs/"
                            + value,
                            headers=headers,
                        ) as source:
                            source.raise_for_status()
                            with temporary.open("wb") as writer:
                                for chunk in source.iter_bytes(1024**2):
                                    writer.write(chunk)
                        assert digest(temporary) == value
                        temporary.rename(target)
                        print("remote verified", value, descriptor["size"], flush=True)
                    assert target.stat().st_size == descriptor["size"] and digest(target) == value
                    required[value] = target

        manifest(args.expected_digest)
    reference = args.repository + ":latest"
    descriptor = {
        **manifests[args.expected_digest],
        "annotations": {
            "io.containerd.image.name": "docker.io/" + reference,
            "org.opencontainers.image.ref.name": reference,
        },
    }
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [descriptor],
    }
    archive_path = output / "official-image.oci.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, document in [
            ("oci-layout", {"imageLayoutVersion": "1.0.0"}),
            ("index.json", index),
        ]:
            content = json.dumps(document).encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for value, path in required.items():
            archive.add(path, arcname="blobs/sha256/" + value.split(":")[1], recursive=False)
    print("load fixed OCI", args.expected_digest, flush=True)
    with archive_path.open("rb") as stream:
        for event in client.api.load_image(stream):
            print(str(event)[:500], flush=True)
    image = client.images.get(reference)
    assert image.id == args.expected_digest, (image.id, args.expected_digest)
    receipt = {
        "repository": args.repository,
        "image_ref": reference,
        "expected_image_id": args.expected_digest,
        "actual_image_id": image.id,
        "reuse_image": args.reuse_image,
        "reuse_image_id": reuse.id,
        "local_blob_count": len(reused),
        "required_blobs": [
            {"digest": value, "bytes": path.stat().st_size} for value, path in required.items()
        ],
        "archive_sha256": digest(archive_path),
        "source": "Official Docker Hub only; anonymous registry token never persisted",
        "model_requests": 0,
    }
    (output / "restore-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    main()
