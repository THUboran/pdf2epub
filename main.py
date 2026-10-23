#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
import modules.pdf2md as pdf2md
import modules.mark2epub as mark2epub
import torch
                


def main():
    if torch.cuda.is_available():
        print("CUDA is available. Using GPU for processing.")
    elif torch.backends.mps.is_available():
        print("MPS is available. Using Apple Silicon for processing.")
    else:
        print("CUDA is not available. Using CPU for processing.")
        
    parser = argparse.ArgumentParser(
        description='Convert PDF files to EPUB format via Markdown'
    )
    parser.add_argument(
        'input_path',
        nargs='?',
        type=str,
        help='Path to input PDF file or directory (default: ./input/*.pdf)'
    )
    parser.add_argument(
        'output_path',
        nargs='?',
        type=str,
        help='Path to output directory (default: directory named after PDF)'
    )
    parser.add_argument(
        '--max-pages',
        type=int,
        default=None,
        help='Maximum number of pages to process'
    )
    parser.add_argument(
        '--start-page',
        type=int,
        default=None,
        help='Page number to start from'
    )
    parser.add_argument(
        '--skip-epub',
        action='store_true',
        help='Skip EPUB generation, only create markdown'
    )
    parser.add_argument(
        '--skip-md',
        action='store_true',
        help='Skip markdown generation, use existing markdown files'
    )
    parser.add_argument(
        '--non-interactive',
        action='store_true',
        help='Never prompt; use defaults from --metadata-file or PDF metadata'
    )
    parser.add_argument(
        '--metadata-file',
        type=str,
        default=None,
        help='JSON file with metadata fields (dc:title, dc:creator, dc:language, ...)'
    )
    
    args = parser.parse_args()

    # 非交互模式下把 metadata 预写成 description.json，供 mark2epub 直接使用
    if args.non_interactive:
        import os as _os
        _os.environ["PDF2EPUB_NONINTERACTIVE"] = "1"

    # Get input path
    input_path = Path(args.input_path) if args.input_path else pdf2md.get_default_input_dir()
    
    # Get queue of PDFs to process
    queue = pdf2md.add_pdfs_to_queue(input_path)
    print(f"Found {len(queue)} PDF files to process")
    
    # Process each PDF
    failed = []
    for pdf_path in queue:
        is_md_only = pdf_path.is_dir()
        label = pdf_path.name
        print(f"\nProcessing: {label}")

        # Get output directory for this PDF
        if args.output_path:
            output_path = Path(args.output_path)
            markdown_dir = output_path / (pdf_path.name if is_md_only else pdf_path.stem)
        elif is_md_only:
            markdown_dir = pdf_path
            output_path = pdf_path.parent
        else:
            markdown_dir = pdf2md.get_default_output_dir(pdf_path)
            output_path = markdown_dir.parent
            
        try:
            # Check if markdown directory exists when skipping MD generation
            if args.skip_md:
                if not markdown_dir.exists():
                    print(f"Error: Markdown directory not found: {markdown_dir}", file=sys.stderr)
                    failed.append(pdf_path.name)
                    continue
                print(f"Using existing markdown files from: {markdown_dir}")
                
            # Convert PDF to Markdown unless skipped
            if not args.skip_md and not is_md_only:
                print("Converting PDF to Markdown...")
                pdf2md.convert_pdf(
                    str(pdf_path),
                    markdown_dir,
                    args.max_pages,
                    args.start_page,
                )
            
            # 非交互 / 指定 metadata 文件时，写出 description.json
            if args.non_interactive or args.metadata_file:
                import json as _json
                preset = {}
                if args.metadata_file:
                    with open(args.metadata_file, "r", encoding="utf-8") as mf:
                        preset = _json.load(mf)
                desc = {
                    "metadata": {
                        "dc:title": preset.get("dc:title") or (pdf_path.name if is_md_only else pdf_path.stem),
                        "dc:creator": preset.get("dc:creator", "Unknown Author"),
                        "dc:identifier": preset.get("dc:identifier", ""),
                        "dc:language": preset.get("dc:language", ""),
                        "dc:rights": preset.get("dc:rights", "All rights reserved"),
                        "dc:publisher": preset.get("dc:publisher", ""),
                        "dc:date": preset.get("dc:date", ""),
                    },
                    "default_css": preset.get("default_css", ["style.css"]),
                    "chapters": [],
                    "cover_image": preset.get("cover_image", None),
                }
                markdown_dir.mkdir(parents=True, exist_ok=True)
                with open(markdown_dir / "description.json", "w", encoding="utf-8") as df:
                    _json.dump(desc, df, ensure_ascii=False, indent=2)

            # Convert Markdown to EPUB unless skipped
            if not args.skip_epub:
                print("Converting Markdown to EPUB...")
                # EPUB 与 markdown 放在同一书目录下（convert_to_epub 接受文件或目录）
                epub_dest = markdown_dir / f"{markdown_dir.name}.epub"
                mark2epub.convert_to_epub(markdown_dir, epub_dest)
                
        except Exception as e:
            print(f"Error processing {pdf_path.name}: {str(e)}", file=sys.stderr)
            failed.append(pdf_path.name)
            continue

    if failed:
        print(
            f"\nFailed to process {len(failed)} of {len(queue)} PDF file(s):",
            file=sys.stderr,
        )
        for name in failed:
            print(f"  - {name}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()