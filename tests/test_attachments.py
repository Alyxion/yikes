from __future__ import annotations

from pathlib import Path

from yikes import AgentSettings, Backend, ChatService, Driver, ImageAttachment
from yikes.attachments import extract_image_attachments, prompt_with_image_references
from yikes.domain import ChatOptions
from yikes import drivers


class AttachmentCaptureTransport:
    def __init__(self) -> None:
        self.attachments: tuple[ImageAttachment, ...] = ()

    def ask(
        self,
        options: ChatOptions,
        prompt: str,
        attachments: tuple[ImageAttachment, ...] = (),
    ) -> str:
        self.attachments = attachments
        return "OK"


def test_extract_image_attachments_from_pasted_path(tmp_path: Path) -> None:
    image = tmp_path / "screen shot.png"
    image.write_bytes(b"fake")

    remaining, attachments = extract_image_attachments(f"Please inspect '{image}'", cwd=tmp_path)

    assert remaining == "Please inspect"
    assert attachments == (ImageAttachment(image),)


def test_conversation_forwards_image_attachments_to_transport(tmp_path: Path) -> None:
    image = tmp_path / "ui.png"
    image.write_bytes(b"fake")
    transport = AttachmentCaptureTransport()
    conversation = ChatService().create_conversation(
        Backend.CLAUDE,
        Driver.DIRECT,
        cwd=tmp_path,
        transport=transport,
    )

    conversation.ask("What is shown here?", (ImageAttachment(image),))

    assert transport.attachments == (ImageAttachment(image),)


def test_claude_prompt_references_host_image_path(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"fake")

    prompt = prompt_with_image_references("Describe this.", (ImageAttachment(image),))

    assert "Describe this." in prompt
    assert f"@{image}" in prompt


def test_codex_shell_command_uses_native_image_flag() -> None:
    command = drivers._backend_shell_command(
        "codex",
        "Describe this.",
        result_path=Path("/tmp/result.txt"),
        err_path=Path("/tmp/err.txt"),
        model=None,
        settings=AgentSettings(),
        attachments=(Path("/workspace/yikes-attachments/image.png"),),
    )

    assert "--image /workspace/yikes-attachments/image.png" in command


def test_docker_attachment_copy_writes_to_workspace(tmp_path: Path) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"fake-image")

    class FakeSandbox:
        def __init__(self) -> None:
            self.writes: dict[str, bytes] = {}

        def exec(self, *_args: object, **_kwargs: object) -> object:
            return object()

        def write_file(self, path: str, content: bytes) -> None:
            self.writes[path] = content

    sandbox = FakeSandbox()

    mapped = drivers._copy_attachments_to_sandbox(sandbox, (ImageAttachment(image),))  # type: ignore[arg-type]

    assert len(mapped) == 1
    assert str(mapped[0]).startswith("/workspace/yikes-attachments/")
    assert sandbox.writes[str(mapped[0])] == b"fake-image"
