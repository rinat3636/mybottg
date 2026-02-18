# Changelog - Nano Banana Bot

## Version 3.1.0 - 2026-02-16

### Critical Fixes
- **Fixed AttributeError in generation handler**: Resolved undefined `update` variable in `_enqueue_generation` function that caused crashes when users had insufficient credits or tried to start a second generation while one was active.
- **Improved hex decoding error handling**: Added proper exception handling for hex-to-bytes conversion in queue worker to prevent task failures from corrupted data.

### User Experience Improvements
- **Enhanced result delivery**: Results are now sent as both a compressed photo (for quick preview) and a document (for original quality).
- **Cleaner error messages**: Removed confusing trace_id from user-facing error messages. Trace IDs are still logged for debugging.

### Technical Improvements
- Better error handling in image data processing
- More robust queue worker implementation
- Improved logging for debugging

### Migration from Google API to Replicate
This version uses Replicate API with the `google/nano-banana-pro` model instead of the previous Google API implementation.

---

## Previous Versions

See FIXES_README.md for earlier fixes and migration notes.
