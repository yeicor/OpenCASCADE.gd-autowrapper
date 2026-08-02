"""Generate Godot XML documentation files from extracted DocBlocks."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from model import ClassDecl, MethodDecl, MethodKind, ModuleDecl
from classify.overloads import get_method_unique_name
from generate.inherit import wrapper_base
from generate.type_map import TypeMap


def generate_doc_xml(cls: ClassDecl, output_dir: Path, type_map: TypeMap | None = None) -> str | None:
    """Generate a Godot XML doc file for a class.

    Returns the XML content if docs were generated, None if no docs.
    """
    # Check if there's any documentation to write
    has_class_doc = cls.doc.brief or cls.doc.raw
    has_method_doc = any(m.doc.brief or m.doc.raw for m in cls.all_wrappable_methods)
    if not has_class_doc and not has_method_doc:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    # A wrapper inherits from its wrapper base class (if any) or RefCounted.
    inherits = "RefCounted"
    if type_map is not None:
        wb = wrapper_base(cls, type_map)
        if wb:
            inherits = wb[0]

    # Build XML
    root = ET.Element("class", name=cls.wrapper_name, inherits=inherits)

    # Brief description
    brief = ET.SubElement(root, "brief_description")
    brief.text = cls.doc.brief or _extract_first_sentence(cls.doc.raw)

    # Full description
    desc = ET.SubElement(root, "description")
    desc.text = cls.doc.raw or cls.doc.brief or f"Wrapper for OCCT class {cls.name}."

    # Methods (must be inside <methods> container for Godot 4).
    # Only emit the container when it has children: an empty self-closing
    # <methods /> element breaks Godot's doc parser, which sees the next
    # <class> element as a nested node inside <methods> and aborts with
    # "Invalid tag in doc file: class, expected method".
    documented_methods = [
        m for m in cls.all_wrappable_methods
        if not m.skip and (m.doc.brief or m.doc.raw)
    ]
    if documented_methods:
        methods_elem = ET.SubElement(root, "methods")
        for method in documented_methods:
            unique_name = get_method_unique_name(method)
            qualifiers = []
            if method.kind == MethodKind.CONSTRUCTOR or method.kind == MethodKind.STATIC_METHOD:
                qualifiers.append("static")
            if method.is_const:
                qualifiers.append("const")

            method_elem = ET.SubElement(methods_elem, "method", name=unique_name)
            if qualifiers:
                method_elem.set("qualifiers", " ".join(qualifiers))

            # Return type
            # (omitting return type element for now — it's complex to map correctly)

            # Description
            mdesc = ET.SubElement(method_elem, "description")
            mdesc.text = method.doc.brief or method.doc.raw or ""

    # Write file
    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    import io
    buf = io.StringIO()
    tree.write(buf, encoding="unicode", xml_declaration=True)

    return buf.getvalue()


def _extract_first_sentence(text: str) -> str:
    """Extract the first sentence from a doc block."""
    if not text:
        return ""
    # Take first line that isn't empty
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line
    return ""
