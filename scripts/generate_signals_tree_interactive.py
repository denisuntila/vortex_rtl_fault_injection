import os
import argparse

def build_interactive_html_tree(input_txt_path: str, output_html_path: str):
    """
    Reads the variable list, reconstructs the hardware hierarchy
    and generates an interactive HTML file with collapsible nodes.
    """
    tree = {}

    def clean_token(token):
        """Makes Verilator tokens readable"""
        token = token.replace("__DOT__", ".")
        token = token.replace("__BRA__", "[")
        token = token.replace("__KET__", "]")
        token = token.replace("_05F", "_")
        return token

    print(f"[HTML Tree] Hierarchical analysis of: {input_txt_path}")

    # 1. Reconstruct the tree using a dictionary
    with open(input_txt_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            parts = line.split("__DOT__")
            current_level = tree
            for part in parts:
                cleaned = clean_token(part)
                if cleaned not in current_level:
                    current_level[cleaned] = {}
                current_level = current_level[cleaned]

    # 2. Recursive function to generate collapsible HTML tags
    def html_render(node):
        html_lines = []
        items = sorted(node.keys())
        
        for item in items:
            if node[item]:  # If it has children, it's a module/structure (collapsible)
                html_lines.append("<li>")
                html_lines.append(f"  <details open>")
                html_lines.append(f"    <summary><strong>{item}</strong></summary>")
                html_lines.append("    <ul>")
                html_lines.extend(f"      {line}" for line in html_render(node[item]))
                html_lines.append("    </ul>")
                html_lines.append("  </details>")
                html_lines.append("</li>")
            else:  # If it has no children, it's a leaf variable (the final signal)
                html_lines.append(f"<li>{item}</li>")
                
        return html_lines

    # 3. HTML page structure without CSS styling
    html_content = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "    <meta charset='UTF-8'>",
        "    <title>Interactive Hardware Hierarchy - Vortex GPU</title>",
        "</head>",
        "<body>",
        "    <h1>Vortex GPU - Hardware Hierarchy Tree</h1>",
        "    <div>",
        "        <button onclick='toggleAll(false)'>Collapse All</button>",
        "        <button onclick='toggleAll(true)'>Expand All</button>",
        "    </div>",
        "    <div>",
        "        <ul>"
    ]

    # Generate the HTML tree body
    html_content.extend(html_render(tree))

    # Add JavaScript for the "Expand All/Collapse All" buttons at the end
    html_content.extend([
        "        </ul>",
        "    </div>",
        "    <script>",
        "        function toggleAll(openState) {",
        "            const details = document.querySelectorAll('details');",
        "            details.forEach(d => d.open = openState);",
        "        }",
        "    </script>",
        "</body>",
        "</html>"
    ])

    # 4. Save the file
    with open(output_html_path, "w", encoding="utf-8") as out_file:
        out_file.write("\n".join(html_content))

    print(f"[HTML Tree] Interactive file successfully generated: '{output_html_path}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML hardware hierarchy tree from a sorted signal names file."
    )
    parser.add_argument(
        "-i", "--input",
        default="./sorted_signal_names.txt",
        help="Path to the input text file containing sorted signal names (default: ./sorted_signal_names.txt)"
    )
    parser.add_argument(
        "-o", "--output",
        default="./index.html",
        help="Path to the output HTML file (default: ./index.html)"
    )
    
    args = parser.parse_args()
    
    if os.path.exists(args.input):
        build_interactive_html_tree(args.input, args.output)
    else:
        print(f"Error: The file '{args.input}' is missing. Generate it first!")