"""Ingestion: raw PDF -> OCR -> cleaned, grouped, chunked text ready to embed.

Pipeline order: probe -> tools_check -> ocr -> score_pages -> clean_pages
-> group_documents -> chunk_documents.
"""
