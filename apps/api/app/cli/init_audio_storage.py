from app.core.config import get_settings
from app.services.audio_storage import AUDIO_BUCKETS, build_audio_storage


def main() -> None:
    storage = build_audio_storage(get_settings())
    storage.ensure_buckets()
    print("private audio buckets ready: " + ", ".join(AUDIO_BUCKETS))


if __name__ == "__main__":
    main()
