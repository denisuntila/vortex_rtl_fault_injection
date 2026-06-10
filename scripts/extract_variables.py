import re
import argparse
from typing import List, Tuple, Optional, Set


class VerilatorCppParser:
    """Parses a Verilator-generated C++ root header file to identify target signals
    and generate a continuous probability mapping for hardware fault injection simulation.
    """

    def __init__(self, file_path: str):
        self.file_path: str = file_path
        self.content: Optional[str] = None
        self.class_content: Optional[str] = None
        # Stores groups of tuples: (data_type, element_bits, array_depth, first_name, consecutive_count)
        self.structs: List[List[Tuple[str, int, int, str, int]]] = []
        # Track written signals to avoid duplicates across multiple struct blocks
        self.written_signals: Set[str] = set()

    def _read_file(self) -> None:
        """Reads the source file content into memory."""
        if self.content is None:
            try:
                with open(self.file_path, "r", encoding="utf-8") as file:
                    self.content = file.read()
            except FileNotFoundError:
                raise FileNotFoundError(f"File '{self.file_path}' does not exist.")
            except Exception as e:
                raise IOError(f"Error reading file: {e}")

    def _find_class(self, class_name: str) -> None:
        """Locates and extracts the internal body of the specified C++ class
        while safely tracking bracket depths and ignoring string/comment scopes.
        """
        if self.content is None:
            print("No content found. Call self._read_file() before parsing.")
            return

        # Matches class definitions including optional alignment specifiers
        class_pattern = rf"class\s+(?:alignas\([^)]+\)\s+)?{class_name}\b[^{{]*{{"
        class_head_match = re.search(class_pattern, self.content)

        if not class_head_match:
            print(f"Class '{class_name}' not found.")
            return

        # Index where the class content block starts (right after the opening curly bracket)
        start_idx = class_head_match.end()

        def blank_out(match: re.Match) -> str:
            return " " * len(match.group(0))

        # Pattern to clean up comments and string literals to prevent bracket parsing errors
        clean_pattern = r"(//.*?$|/\*.*?\*/|'(?:\\.|[^\\'])*'|\"(?:\\.|[^\\\"])*\")"
        working_content = re.sub(clean_pattern, blank_out, self.content, flags=re.MULTILINE)

        idx = start_idx
        length = len(working_content)
        curly_brackets_counter = 1

        # Scan through the sanitized copy to safely find the closing bracket of the class
        while idx < length and curly_brackets_counter > 0:
            char = working_content[idx]
            if char == "{":
                curly_brackets_counter += 1
            elif char == "}":
                curly_brackets_counter -= 1
            idx += 1

        if curly_brackets_counter == 0:
            # Step back by 1 since the loop increments right after finding the character
            end_idx = idx - 1 
            self.class_content = self.content[start_idx:end_idx]
        else:
            print(f"Warning: Reached EOF while parsing class '{class_name}'. Malformed brackets?")
            self.class_content = self.content[start_idx:]

    def _group_consecutive_vortex_lines(
        self, 
        input_text: str, 
        signal_file=None
    ) -> List[Tuple[str, int, int, str, int]]:
        """Parses individual statements inside a struct block, aggregates contiguous 
        matching metadata primitives, and filters out non-target hardware lines.
        """
        pattern_unpacked = re.compile(
            r"\bVlUnpacked<\s*(\w+)\s*/\*(\d+):(\d+)\*/\s*,\s*(\d+)>\s+([^;{=]+)"
        )
        pattern_scalar = re.compile(
            r"\b([\w<>]+)\s*/\*(\d+):(\d+)\*/\s+([^;{=]+)"
        )

        results = []
        current_type = None
        current_bits = None
        current_size = None
        first_name = None
        count = 0

        type_mapper = {
            "CData": "CDATA_8",
            "SData": "SDATA_16",
            "IData": "IDATA_32",
            "QData": "QDATA_64",
            "VlWide": "WIDE_512"
        }

        statements = input_text.split(";")

        for statement in statements:
            statement = statement.strip()

            if "vortex" not in statement:
                if first_name is not None:
                    results.append((current_type, current_bits, current_size, first_name, count))
                    current_type = current_bits = current_size = first_name = None
                    count = 0
                continue

            # Unpacked Array Check
            match = pattern_unpacked.search(statement)
            if match:
                raw_type, msb, lsb, size_arr, name = match.groups()
                raw_type_clean = raw_type.split("<")[0]
                element_bits = abs(int(msb) - int(lsb)) + 1
                data_type = type_mapper.get(raw_type_clean, "IDATA_32")
                size = int(size_arr)
            else:
                # Scalar or Wide Data Check
                match = pattern_scalar.search(statement)
                if not match:
                    continue

                raw_type, msb, lsb, name = match.groups()
                raw_type_clean = raw_type.split("<")[0]
                element_bits = abs(int(msb) - int(lsb)) + 1
                data_type = type_mapper.get(raw_type_clean, "IDATA_32")
                size = 1

            var_name = name.strip()

            # Hardware filters to ignore internal control signals
            is_ignored_signal = (
                "unused" in var_name or
                "_05Funused" in var_name or
                var_name.endswith("clk") or
                "_05Fclk" in var_name or
                var_name.endswith("reset") or
                var_name.endswith("rst") or
                "_05Freset" in var_name or
                "_05Frst" in var_name or
                "__V" in var_name
            )

            if is_ignored_signal:
                if first_name is not None:
                    results.append((current_type, current_bits, current_size, first_name, count))
                    current_type = current_bits = current_size = first_name = None
                    count = 0
                continue

            # Write signal names to debug dump if requested
            if signal_file is not None and var_name not in self.written_signals:
                signal_file.write(f"{var_name}\n")
                self.written_signals.add(var_name)

            # Grouping evaluation logic
            if (
                data_type == current_type and
                element_bits == current_bits and
                size == current_size
            ):
                count += 1
            else:
                if first_name is not None:
                    results.append((current_type, current_bits, current_size, first_name, count))

                current_type = data_type
                current_bits = element_bits
                current_size = size
                first_name = var_name
                count = 1

        if first_name is not None:
            results.append((current_type, current_bits, current_size, first_name, count))

        return results

    def _find_anonymous_structs_rows(self, debug_output_path: str = "valid_signals_extracted.txt") -> None:
        """Locates anonymous structures embedded inside the Verilator class scope
        and processes their signals for the fault ruler array.
        """
        if self.class_content is None:
            print("Class content is empty. Ensure self._find_class() runs successfully.")
            return

        content = self.class_content
        length = len(content)

        def blank_out(match: re.Match) -> str:
            return " " * len(match.group(0))

        clean_pattern = r"(//.*?$|/\*.*?\*/|'(?:\\.|[^\\'])*'|\"(?:\\.|[^\\\"])*\")"
        working_content = re.sub(clean_pattern, blank_out, content, flags=re.MULTILINE)

        struct_stack = []  
        brace_depth = 0    

        print(f"[RTL PARSER] Writing extracted valid signals to: {debug_output_path}")
        with open(debug_output_path, "w", encoding="utf-8") as debug_file:
            idx = 0
            while idx < length:
                if working_content.startswith("struct", idx):
                    match = re.match(r"^struct\s*\{", working_content[idx:])
                    if match:
                        start_content_idx = idx + match.end()
                        struct_stack.append((start_content_idx, brace_depth))
                        brace_depth += 1
                        idx += match.end()
                        continue

                char = working_content[idx]

                if char == "{":
                    brace_depth += 1
                elif char == "}":
                    brace_depth -= 1

                    if struct_stack and brace_depth == struct_stack[-1][1]:
                        start_idx, _ = struct_stack.pop()
                        end_idx = idx

                        struct_content = content[start_idx:end_idx]
                        grouped_lines = self._group_consecutive_vortex_lines(
                            struct_content,
                            signal_file=debug_file
                        )
                        self.structs.append(grouped_lines)
                idx += 1

        # Diagnostic output analysis summary
        total_targets = sum(len(block) for block in self.structs)
        total_bits = sum(r[1] * r[2] * r[4] for block in self.structs for r in block)
        print(f"[RTL PARSER] Generated {total_targets} total valid fault target blocks.")
        print(f"[RTL PARSER] Calculated SEU-sensitive area: {total_bits} bits.")

    def generate_cpp_file(self, output_path: str = "fault_targets.cpp") -> None:
        """Generates the final fault target configuration layout matching the C++ runtime environment.
        Preserves hardware geometry and exports global metadata tracking information.
        """
        cpp_lines = [
            '#include "fault_injection.h"',
            '#include "Vrtlsim_shim___024root.h"',
            '#include <cstddef>\n',
            'const FaultTarget FAULT_RULER[] = {'
        ]

        cumulative_bits = 0
        total_targets_written = 0

        for struct_block in self.structs:
            if not struct_block:
                continue
                
            for data_type, element_bits, array_depth, first_name, consecutive_count in struct_block:
                # Accumulate the true hardware bits inside the scale
                block_total_bits = element_bits * array_depth * consecutive_count
                cumulative_bits += block_total_bits
                total_targets_written += 1

                # Generate C++20 designated structural initialization block
                entry = (
                    f"    {{\n"
                    f"        .offset = offsetof(Vrtlsim_shim___024root, {first_name}),\n"
                    f"        .bit_reali_elem = {element_bits},\n"
                    f"        .array_depth = {array_depth},\n"
                    f"        .consecutive_count = {consecutive_count},\n"
                    f"        .type = {data_type},\n"
                    f"        .max_cumulato = {cumulative_bits}\n"
                    f"    }},"
                )
                cpp_lines.append(entry)

        cpp_lines.append("};\n")
        cpp_lines.append(f"const size_t FAULT_RULER_SIZE = {total_targets_written};")
        cpp_lines.append(f"const uint32_t BIT_TOTALI_VORTEX = {cumulative_bits};")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(cpp_lines))
            
        print(f"[Python Parser] Successfully generated implementation target: '{output_path}'")


if __name__ == "__main__":
    cli_parser = argparse.ArgumentParser(
        description="Parse a Verilator root header and generate fault_targets.cpp configurations."
    )
    cli_parser.add_argument(
        "input_file",
        help="Path to Vrtlsim_shim___024root.h source"
    )
    cli_parser.add_argument(
        "output_file",
        help="Target output path for the generated fault_targets.cpp layout"
    )
    cli_parser.add_argument(
        "--class-name",
        default="Vrtlsim_shim___024root",
        help="Target implementation class name scope (default: Vrtlsim_shim___024root)"
    )
    cli_parser.add_argument(
        "--debug-file",
        default="valid_signals_extracted.txt",
        help="Output target file path containing extracted hardware signal dumps"
    )

    args = cli_parser.parse_args()

    parser = VerilatorCppParser(args.input_file)
    
    parser._read_file()
    print("[Pipeline] Input file loaded successfully.")

    parser._find_class(args.class_name)
    print(f"[Pipeline] Targeted class context scope structural parsing complete.")

    parser._find_anonymous_structs_rows(args.debug_file)
    print("[Pipeline] Core metadata signals successfully mapped from code structures.")

    parser.generate_cpp_file(args.output_file)





