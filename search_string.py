import os

TARGET_STRING = "<agent_image>"
ROOT_DIR = "./"
CONTEXT_LINES = 0
FILE_EXTENSION = ".json"

def is_binary(file_path):
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(1024)
        return b'\x00' in chunk
    except OSError:
        return True

def read_lines_safe(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.readlines()
    except (UnicodeDecodeError, PermissionError, OSError):
        return None

def main():
    total_matches = 0
    files_scanned = 0

    for root, dirs, files in os.walk(ROOT_DIR):
        for file_name in files:
            if FILE_EXTENSION:
                if not file_name.lower().endswith(FILE_EXTENSION.lower()):
                    continue
            file_path = os.path.join(root, file_name)
            if is_binary(file_path):
                continue
            lines = read_lines_safe(file_path)
            if lines is None:
                continue
            files_scanned += 1
            file_printed = False
            for i, line in enumerate(lines):
                if TARGET_STRING in line:
                    total_matches += 1
                    if not file_printed:
                        print(f"FILE: {file_path}")
                        file_printed = True

                    start = max(0, i - CONTEXT_LINES)
                    end = min(len(lines), i + CONTEXT_LINES + 1)

                    print(f"--- line {i + 1} ---")
                    for context_line in lines[start:end]:
                        print(f"    {context_line.rstrip()}")
                    print()
    print(f"扫描了 {files_scanned} 个文本文件，共 {total_matches} 处匹配。")

if __name__ == "__main__":
    main()