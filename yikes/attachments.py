from __future__ import annotations

import os
import re
import shutil
import shlex
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .domain import ImageAttachment


IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def image_attachment(path: Path | str, *, cwd: Path | None = None) -> ImageAttachment:
    resolved = resolve_image_path(path, cwd=cwd)
    if resolved is None:
        raise ValueError(f"Not an image file: {path}")
    return ImageAttachment(resolved)


def resolve_image_path(path: Path | str, *, cwd: Path | None = None) -> Path | None:
    raw = _path_from_token(str(path))
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and cwd is not None:
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return resolved


def extract_image_attachments(text: str, *, cwd: Path) -> tuple[str, tuple[ImageAttachment, ...]]:
    attachments: list[ImageAttachment] = []
    remaining = text
    for token in _candidate_tokens(text):
        path = resolve_image_path(token, cwd=cwd)
        if path is None:
            continue
        attachment = ImageAttachment(path)
        if attachment not in attachments:
            attachments.append(attachment)
        remaining = remaining.replace(_quote_token(token), " ")
        remaining = remaining.replace(token, " ")
    return _collapse_text(remaining), tuple(attachments)


def attachable_image_names(attachments: tuple[ImageAttachment, ...]) -> str:
    return ", ".join(attachment.name for attachment in attachments)


def save_clipboard_image(*, target_dir: Path | None = None) -> ImageAttachment | None:
    target = (target_dir or (Path.home() / ".yikes" / "attachments")).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    output = target / f"clipboard-{uuid4().hex}.png"
    if _save_clipboard_with_pngpaste(output):
        return ImageAttachment(output)
    if _save_clipboard_with_osascript(output):
        return ImageAttachment(output)
    if _save_clipboard_with_wl_paste(output):
        return ImageAttachment(output)
    if _save_clipboard_with_xclip(output):
        return ImageAttachment(output)
    output.unlink(missing_ok=True)
    return None


def read_clipboard_text() -> str | None:
    if _command_exists("pbpaste"):
        return _read_clipboard_command(["pbpaste"])
    if _command_exists("wl-paste"):
        return _read_clipboard_command(["wl-paste", "--type", "text/plain"])
    if _command_exists("xclip"):
        return _read_clipboard_command(["xclip", "-selection", "clipboard", "-o"])
    return None


def prompt_with_image_references(prompt: str, attachments: tuple[ImageAttachment, ...]) -> str:
    if not attachments:
        return prompt
    refs = "\n".join(f"@{attachment.path}" for attachment in attachments)
    return f"{prompt}\n\nAttached image files:\n{refs}"


def prompt_with_mapped_image_references(
    prompt: str,
    mapped_paths: tuple[Path, ...],
) -> str:
    if not mapped_paths:
        return prompt
    refs = "\n".join(f"@{path}" for path in mapped_paths)
    return f"{prompt}\n\nAttached image files:\n{refs}"


def _candidate_tokens(text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text))
    candidates.extend(match.group(0).strip() for match in re.finditer(r"file://[^\s)]+", text))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            candidates.append(stripped)
    try:
        candidates.extend(shlex.split(text))
    except ValueError:
        pass
    return [_unquote_token(candidate) for candidate in candidates if candidate.strip()]


def _path_from_token(token: str) -> str:
    value = _unquote_token(token)
    if value.startswith("file://"):
        parsed = urlparse(value)
        return unquote(parsed.path)
    return value


def _unquote_token(token: str) -> str:
    return token.strip().strip("'\"")


def _quote_token(token: str) -> str:
    return shlex.quote(token)


def _collapse_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _save_clipboard_with_pngpaste(output: Path) -> bool:
    if not _command_exists("pngpaste"):
        return False
    result = subprocess.run(["pngpaste", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def _save_clipboard_with_osascript(output: Path) -> bool:
    if os.name != "posix" or not _command_exists("osascript"):
        return False
    escaped = str(output).replace('"', '\\"')
    script = f'''
set outPath to POSIX file "{escaped}"
try
    set pngData to the clipboard as «class PNGf»
on error
    return "no-image"
end try
set fileRef to open for access outPath with write permission
try
    set eof fileRef to 0
    write pngData to fileRef
    close access fileRef
    return "ok"
on error errText
    try
        close access fileRef
    end try
    error errText
end try
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def _save_clipboard_with_wl_paste(output: Path) -> bool:
    if not _command_exists("wl-paste"):
        return False
    with output.open("wb") as file:
        result = subprocess.run(["wl-paste", "--type", "image/png"], stdout=file, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def _save_clipboard_with_xclip(output: Path) -> bool:
    if not _command_exists("xclip"):
        return False
    with output.open("wb") as file:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            stdout=file,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def _read_clipboard_command(argv: list[str]) -> str | None:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _command_exists(command: str) -> bool:
    return shutil.which(command) is not None
