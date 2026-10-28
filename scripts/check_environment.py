"""Offline diagnostics; never loads credentials or opens production runtime data."""
import argparse
import asyncio
import importlib
import importlib.metadata
import sys
from pathlib import Path
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def ocr_smoke():
    from PIL import Image, ImageDraw
    from telegram_bot.services.jr.ocr_service import OCRService
    from telegram_bot.models.enums import Outcome
    image = Image.new('RGB', (200, 80), 'white')
    ImageDraw.Draw(image).text((20, 25), '1234', fill='black')
    output = BytesIO()
    image.save(output, format='PNG')
    service = OCRService(timeout=30)
    try:
        result = await service.recognize(output.getvalue(), charset='1234567890')
        print(f'OCR model inference: {result.outcome.value}, {result.error_type or "OK"}')
        return result.outcome == Outcome.SUCCESS or result.error_type == 'LowConfidence'
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ocr-smoke', action='store_true', help='Run the local model on a synthetic image')
    args = parser.parse_args()
    print('Python:', sys.version.split()[0])
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        print('Supported Python versions: 3.10–3.12; verified on 3.12.14')
        return 1
    modules = {'Telethon': 'telethon', 'python-telegram-bot': 'telegram', 'aiosqlite': 'aiosqlite',
               'Pillow': 'PIL', 'ddddocr': 'ddddocr', 'onnxruntime': 'onnxruntime'}
    failed = False
    for package, module in modules.items():
        try:
            importlib.import_module(module)
            print(f'{package}: {importlib.metadata.version(package)} (import OK)')
        except Exception as error:
            failed = True
            print(f'{package}: import FAILED ({type(error).__name__})')
    if args.ocr_smoke and not failed:
        failed = not asyncio.run(ocr_smoke())
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
