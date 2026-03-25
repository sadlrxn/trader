"""Generate gRPC Python bindings from local proto files when required."""

from __future__ import annotations

from pathlib import Path

from grpc_tools import protoc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROTO_ROOT = _PROJECT_ROOT / "proto"
_OUTPUT_ROOT = Path(__file__).resolve().parent / "grpc_gen"
_PROTO_TARGETS: tuple[tuple[Path, tuple[Path, ...]], ...] = (
    (
        _PROTO_ROOT / "trader.proto",
        (
            _OUTPUT_ROOT / "trader_pb2.py",
            _OUTPUT_ROOT / "trader_pb2_grpc.py",
        ),
    ),
    (
        _PROTO_ROOT / "grpc" / "reflection" / "v1alpha" / "reflection.proto",
        (
            _OUTPUT_ROOT / "grpc" / "reflection" / "v1alpha" / "reflection_pb2.py",
            _OUTPUT_ROOT / "grpc" / "reflection" / "v1alpha" / "reflection_pb2_grpc.py",
        ),
    ),
)


def ensure_generated() -> None:
    """Regenerate gRPC bindings when the proto sources are newer than the outputs."""

    if not _needs_regeneration():
        return
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{_PROTO_ROOT}",
            f"--python_out={_OUTPUT_ROOT}",
            f"--grpc_python_out={_OUTPUT_ROOT}",
            "trader.proto",
            "grpc/reflection/v1alpha/reflection.proto",
        ]
    )
    if result != 0:
        raise RuntimeError("Failed to generate Python bindings from proto files.")


def _needs_regeneration() -> bool:
    """Return whether any generated binding is missing or older than its proto source."""

    for proto_path, outputs in _PROTO_TARGETS:
        if any(not output.exists() for output in outputs):
            return True
        latest_output_mtime = min(output.stat().st_mtime for output in outputs)
        if proto_path.stat().st_mtime > latest_output_mtime:
            return True
    return False
