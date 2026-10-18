"""File body ceiling shared by edit and upload.

Wings defaults api.upload_limit to 100 MiB, and the sample nginx config is
client_max_body_size 100m. NodeRings uses that same ceiling.
"""

MAX_FILE_BYTES = 100 * 1024 * 1024


def within_file_limit(size: int) -> bool:
    return 0 <= size <= MAX_FILE_BYTES
