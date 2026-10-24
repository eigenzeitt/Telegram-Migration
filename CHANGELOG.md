# Changelog

## v8.2

- Raises the configured oversize-file threshold to 2000 MiB to match the MTProto upload ceiling described by the project.
- Keeps clean-stop and manual-upload instructions for messages containing files above the configured limit.
- Hardens message-entity validation and limits formatting fallback to recognized entity-bound errors.
- Records single-captioned media state after a successful upload to improve resume behavior.
- Sanitizes archived reaction notes before sending them as text.

## v8.1

- Persists the single-captioned media send state after a successful upload.
- Narrows entity-error detection to avoid misclassifying unrelated exceptions.
- Sanitizes reaction-archive notes and disables parse-mode interpretation for them.

## v8

- Automatically records the source ID of an oversized message when the run stops, allowing a hand-uploaded message to be skipped on resume.

## v7 and earlier

- Adds oversize-media checks, entity bounds hardening, plain-text fallback for rejected formatting entities, FloodWait handling, media-classification updates, hachoir integration, streaming export parsing, and deferred reply reconstruction.
