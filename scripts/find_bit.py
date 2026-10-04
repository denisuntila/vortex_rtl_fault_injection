import re
import argparse
import json
import sys
import bisect
from typing import List, Dict, Optional, Tuple, Set


class VerilatorBitMapper:
    """Mirrors the exact parsing/grouping pipeline used by the fault_targets.cpp
    generator (_find_class -> _find_anonymous_structs_rows -> _group_consecutive_vortex_lines)
    so that global bit indices line up 1:1 with FAULT_RULER entries.

    Key difference from the generator: the C++ FAULT_RULER only stores
    `first_name` + `consecutive_count` per merged run (that's all the injector
    needs, since it flips bits via offsetof(first_name) + n*element_bits).
    But that means the *individual* signal name of the 2nd, 3rd, ... member of
    a run is not recoverable from the ruler itself. Here, since we're not
    constrained by the C++ struct layout, we additionally keep the full list
    of names in each run so query_bit() can report the real signal name.
    """

    def __init__(self, file_path: str):
        self.file_path: str = file_path
        self.content: Optional[str] = None
        self.class_content: Optional[str] = None

        # One entry per FAULT_RULER row (a "run" of consecutive same-shape signals)
        self.memory_map: List[Dict] = []
        self.start_bits: List[int] = []
        self.total_bits: int = 0

    def _log(self, message: str) -> None:
        sys.stderr.write(f"{message}\n")

    def _read_file(self) -> None:
        try:
            with open(self.file_path, "r", encoding="utf-8") as file:
                self.content = file.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"File '{self.file_path}' does not exist.")
        except Exception as e:
            raise IOError(f"Error reading file: {e}")

    def _find_class(self, class_name: str) -> None:
        """Identical to the generator's _find_class."""
        if self.content is None:
            raise ValueError("No content found. Call _read_file() before parsing.")

        class_pattern = rf"class\s+(?:alignas\([^)]+\)\s+)?{class_name}\b[^{{]*{{"
        class_head_match = re.search(class_pattern, self.content)

        if not class_head_match:
            raise ValueError(f"Class '{class_name}' not found.")

        start_idx = class_head_match.end()

        def blank_out(match: re.Match) -> str:
            return " " * len(match.group(0))

        clean_pattern = r"(//.*?$|/\*.*?\*/|'(?:\\.|[^\\'])*'|\"(?:\\.|[^\\\"])*\")"
        working_content = re.sub(clean_pattern, blank_out, self.content, flags=re.MULTILINE)

        idx = start_idx
        length = len(working_content)
        curly_brackets_counter = 1

        while idx < length and curly_brackets_counter > 0:
            char = working_content[idx]
            if char == "{":
                curly_brackets_counter += 1
            elif char == "}":
                curly_brackets_counter -= 1
            idx += 1

        if curly_brackets_counter == 0:
            end_idx = idx - 1
            self.class_content = self.content[start_idx:end_idx]
        else:
            self._log(f"[Warning] Reached EOF while parsing class '{class_name}'.")
            self.class_content = self.content[start_idx:]

    def _group_consecutive_vortex_lines(
        self, input_text: str
    ) -> List[Tuple[str, int, int, List[str], int]]:
        """Same grouping algorithm as the generator's _group_consecutive_vortex_lines,
        but returns the full list of names per run instead of just first_name.
        Return tuple shape: (data_type, element_bits, array_depth, names, consecutive_count)
        """
        pattern_unpacked = re.compile(
            r"\bVlUnpacked<\s*(\w+)\s*/\*(\d+):(\d+)\*/\s*,\s*(\d+)>\s+([^;{=]+)"
        )
        pattern_scalar = re.compile(
            r"\b([\w<>]+)\s*/\*(\d+):(\d+)\*/\s+([^;{=]+)"
        )

        type_mapper = {
            "CData": "CDATA_8",
            "SData": "SDATA_16",
            "IData": "IDATA_32",
            "QData": "QDATA_64",
            "VlWide": "WIDE_512"
        }

        results = []
        current_type = None
        current_bits = None
        current_size = None
        names: List[str] = []

        def flush():
            if names:
                results.append((current_type, current_bits, current_size, names.copy(), len(names)))
            names.clear()

        statements = input_text.split(";")

        for statement in statements:
            statement = statement.strip()

            if "vortex" not in statement:
                flush()
                current_type_local = None
                continue

            match = pattern_unpacked.search(statement)
            if match:
                raw_type, msb, lsb, size_arr, name = match.groups()
                raw_type_clean = raw_type.split("<")[0]
                element_bits = abs(int(msb) - int(lsb)) + 1
                data_type = type_mapper.get(raw_type_clean, "IDATA_32")
                size = int(size_arr)
            else:
                match = pattern_scalar.search(statement)
                if not match:
                    continue
                raw_type, msb, lsb, name = match.groups()
                raw_type_clean = raw_type.split("<")[0]
                element_bits = abs(int(msb) - int(lsb)) + 1
                data_type = type_mapper.get(raw_type_clean, "IDATA_32")
                size = 1

            var_name = name.strip()

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
                flush()
                continue

            if (
                names and
                data_type == current_type and
                element_bits == current_bits and
                size == current_size
            ):
                names.append(var_name)
            else:
                flush()
                current_type = data_type
                current_bits = element_bits
                current_size = size
                names.append(var_name)

        flush()
        return results

    def build_memory_map(self, class_name: str) -> None:
        """Mirrors _find_anonymous_structs_rows exactly (same struct traversal,
        same per-struct-closing processing order), then lays out global bits
        run-by-run in the exact order the generator appends to self.structs.
        """
        self._read_file()
        self._find_class(class_name)

        content = self.class_content
        length = len(content)

        def blank_out(match: re.Match) -> str:
            return " " * len(match.group(0))

        clean_pattern = r"(//.*?$|/\*.*?\*/|'(?:\\.|[^\\'])*'|\"(?:\\.|[^\\\"])*\")"
        working_content = re.sub(clean_pattern, blank_out, content, flags=re.MULTILINE)

        struct_stack = []
        brace_depth = 0
        runs: List[Tuple[str, int, int, List[str], int]] = []

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
                    grouped = self._group_consecutive_vortex_lines(struct_content)
                    runs.extend(grouped)

            idx += 1

        current_global_bit = 0
        for data_type, element_bits, array_depth, names, consecutive_count in runs:
            bits_per_signal = element_bits * array_depth
            run_total_bits = bits_per_signal * consecutive_count

            entry = {
                "start_bit": current_global_bit,
                "end_bit": current_global_bit + run_total_bits - 1,
                "first_name": names[0],
                "names": names,  # extra info not present in FAULT_RULER itself
                "type": data_type,
                "element_bits": element_bits,
                "array_depth": array_depth,
                "consecutive_count": consecutive_count,
            }

            self.memory_map.append(entry)
            self.start_bits.append(current_global_bit)

            current_global_bit += run_total_bits

        self.total_bits = current_global_bit
        self._log(
            f"[Mapper] Map built: {len(self.memory_map)} FAULT_RULER entries, "
            f"{self.total_bits} total SEU-sensitive bits mapped."
        )

    def query_bit(self, target_bit: int) -> Dict:
        if target_bit < 0 or target_bit >= self.total_bits:
            return {
                "global_bit_index": target_bit,
                "error": f"Bit index out of bounds. Valid range: [0, {self.total_bits - 1}]"
            }

        idx = bisect.bisect_right(self.start_bits, target_bit) - 1
        entry = self.memory_map[idx]

        bits_per_signal = entry["element_bits"] * entry["array_depth"]
        bit_offset_in_run = target_bit - entry["start_bit"]

        # Which of the `consecutive_count` merged signals this bit falls into
        consecutive_index = bit_offset_in_run // bits_per_signal
        rem = bit_offset_in_run % bits_per_signal
        element_idx = rem // entry["element_bits"]
        internal_bit = rem % entry["element_bits"]

        signal_name = entry["names"][consecutive_index]

        return {
            "global_bit_index": target_bit,
            "signal_name": signal_name,
            "consecutive_index": consecutive_index,
            "element_index": element_idx,
            "internal_bit_index": internal_bit,
            "type": entry["type"],
            "element_bits": entry["element_bits"],
            "fault_ruler_first_name": entry["first_name"],
            "fault_ruler_consecutive_count": entry["consecutive_count"],
        }

    # ------------------------------------------------------------------
    # Legacy mode: what the OLD injector (before the fix) really flipped.
    #   - entry lookup: first entry with max_cumulato >= dart (comparator `<`),
    #     so a dart equal to a run's end hits one element past that run;
    #   - WIDE_512: element stride fixed at 16 words instead of N of VlWide<N>.
    # The physical position is mapped back to a signal assuming members are
    # laid out contiguously in declaration order (true inside a run of
    # same-shape members); past the end of a run the following declarations
    # of the header are walked with natural C++ alignment (best effort).
    # ------------------------------------------------------------------

    _SIZES = {"CData": (1, 1), "SData": (2, 2), "IData": (4, 4), "QData": (8, 8)}

    def _decl_size(self, decl: str):
        """(size, align) in bytes of a member declaration, or None if unknown."""
        m = re.match(r"\s*VlUnpacked<\s*(.+?)\s*,\s*(\d+)\s*>\s+\w+\s*$", decl)
        if m:
            inner = self._decl_size(m.group(1) + " x")
            return None if inner is None else (inner[0] * int(m.group(2)), inner[1])
        m = re.match(r"\s*VlWide<(\d+)>", decl)
        if m:
            return (4 * int(m.group(1)), 4)
        m = re.match(r"\s*(CData|SData|IData|QData)\b", decl)
        if m:
            return self._SIZES[m.group(1)]
        if re.search(r"\*\s*\w+\s*$", decl):
            return (8, 8)
        return None

    def _member_decls(self):
        """Ordered list of (name, decl) for the member statements of the class."""
        if getattr(self, "_decls", None) is None:
            # drop comments but keep Verilator's /*msb:lsb*/ width annotations
            clean = re.sub(r"/\*(?!\d+:\d+\*/).*?\*/|//[^\n]*", " ", self.class_content, flags=re.S)
            decls = []
            for st in clean.split(";"):
                st = re.sub(r"(struct\s*\{|\})", " ", st).strip()
                if not st or "(" in st or st.startswith(("static", "public", "private", "friend", "using")):
                    continue
                m = re.search(r"(\w+)\s*$", st)
                if m:
                    decls.append((m.group(1), st))
            self._decls = decls
            self._decl_index = {n: i for i, (n, _) in enumerate(decls)}
        return self._decls

    def _resolve_past(self, last_name: str, byte_past_end: int, bit_in_byte: int) -> Dict:
        """What lies `byte_past_end` bytes after the end of member `last_name`."""
        decls = self._member_decls()
        i = self._decl_index.get(last_name)
        if i is None:
            return {"real_signal_name": None, "note": "layout unknown"}
        pos = 0  # bytes from the end of last_name (run ends are 4-byte aligned)
        for name, decl in decls[i + 1:]:
            sz = self._decl_size(decl)
            if sz is None:
                return {"real_signal_name": None, "note": f"layout unknown at {name}"}
            size, align = sz
            pad = (-pos) % align
            if byte_past_end < pos + pad:
                return {"real_signal_name": "<padding>", "note": "padding between members"}
            pos += pad
            if byte_past_end < pos + size:
                return {"real_signal_name": name, "real_element": 0,
                        "real_bit": (byte_past_end - pos) * 8 + bit_in_byte,
                        "note": "outside the labelled run"}
            pos += size
        return {"real_signal_name": None, "note": "past the end of the class"}

    def query_bit_legacy(self, target_bit: int) -> Dict:
        base = self.query_bit(target_bit)
        if "error" in base:
            return base
        # old lookup: first entry whose max_cumulato (= end_bit + 1) >= dart
        if getattr(self, "_ends", None) is None:
            self._ends = [e["end_bit"] + 1 for e in self.memory_map]
        k = bisect.bisect_left(self._ends, target_bit)
        entry = self.memory_map[k]
        local = target_bit - entry["start_bit"]          # may equal the run size
        per = entry["element_bits"] * entry["array_depth"]
        cons, rem = divmod(local, per)
        row, ib = divmod(rem, entry["element_bits"])
        flat = cons * entry["array_depth"] + row
        n_elem = entry["array_depth"] * entry["consecutive_count"]

        if entry["type"] == "WIDE_512":
            words = (entry["element_bits"] + 31) // 32
            word, bitw = flat * 16 + ib // 32, ib % 32
            run_words = words * n_elem
            if word < run_words:
                f2 = word // words
                real = {"real_signal_name": entry["names"][f2 // entry["array_depth"]],
                        "real_element": f2 % entry["array_depth"],
                        "real_bit": (word % words) * 32 + bitw}
            else:
                real = self._resolve_past(entry["names"][-1],
                                          (word - run_words) * 4 + bitw // 8, bitw % 8)
        else:
            nbytes = {"CDATA_8": 1, "SDATA_16": 2, "IDATA_32": 4, "QDATA_64": 8}[entry["type"]]
            if flat < n_elem:
                real = {"real_signal_name": entry["names"][cons], "real_element": row, "real_bit": ib}
            else:
                real = self._resolve_past(entry["names"][-1],
                                          (flat - n_elem) * nbytes + ib // 8, ib % 8)

        out = dict(base)
        out.update(real)
        out["mis_targeted"] = (real.get("real_signal_name") != base["signal_name"]
                               or real.get("real_element") != base["element_index"]
                               or real.get("real_bit") != base["internal_bit_index"])
        return out


if __name__ == "__main__":
    cli_parser = argparse.ArgumentParser(
        description="Query the specific hardware signal associated with a global bit index, "
                     "matching FAULT_RULER's bit layout exactly."
    )
    cli_parser.add_argument("input_file", help="Path to Vrtlsim_shim___024root.h source")
    cli_parser.add_argument(
        "-b", "--bits", required=True,
        help="Comma-separated list of global bit indices to query (e.g., '184320,184321')"
    )
    cli_parser.add_argument(
        "--class-name", default="Vrtlsim_shim___024root",
        help="Target implementation class name scope (default: Vrtlsim_shim___024root)"
    )
    cli_parser.add_argument(
        "--legacy", action="store_true",
        help="Report what the OLD injector (fixed 16-word WIDE stride, off-by-one "
             "ruler lookup) really flipped: real_signal_name / real_bit / mis_targeted"
    )

    args = cli_parser.parse_args()

    try:
        bit_queries = [int(b.strip()) for b in args.bits.split(",")]
    except ValueError:
        sys.stderr.write("Error: --bits must be a comma-separated list of integers.\n")
        sys.exit(1)

    mapper = VerilatorBitMapper(args.input_file)
    try:
        mapper.build_memory_map(args.class_name)
    except Exception as e:
        sys.stderr.write(f"Failed to build memory map: {e}\n")
        sys.exit(1)

    query = mapper.query_bit_legacy if args.legacy else mapper.query_bit
    results = [query(b) for b in bit_queries]
    print(json.dumps(results, indent=2))


    