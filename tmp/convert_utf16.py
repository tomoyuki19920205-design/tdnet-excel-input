import sys
import os

def convert_utf16_to_utf8(input_path, output_path):
    try:
        with open(input_path, 'rb') as f:
            content = f.read()
        
        # Try to detect/decode as UTF-16LE
        text = content.decode('utf-16-le')
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"Successfully converted {input_path} to {output_path}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    convert_utf16_to_utf8(
        r"C:\Users\takuy\OneDrive\tdnet-excel-input\result_partial_review.txt",
        r"C:\Users\takuy\OneDrive\tdnet-excel-input\tmp\result_partial_review_utf8.txt"
    )
    convert_utf16_to_utf8(
        r"C:\Users\takuy\OneDrive\tdnet-excel-input\result_partial.txt",
        r"C:\Users\takuy\OneDrive\tdnet-excel-input\tmp\result_partial_utf8.txt"
    )
