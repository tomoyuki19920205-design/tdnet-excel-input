"""Resolve context resources shared by files within one TDnet XBRL package."""
from __future__ import annotations

from xml.etree import ElementTree as ET

from .xbrl_clean import read_xbrl_bytes
from .xbrl_context_scope import parse_context_metadata, local_name


def resource_metadata(root: ET.Element) -> tuple[dict, dict]:
    contexts = parse_context_metadata(root)
    fields = {
        'CurrentFiscalYearStartDateDEI': 'fiscal_start',
        'CurrentFiscalYearEndDateDEI': 'fiscal_end',
        'FiscalYearEnd': 'fiscal_end',
        'WhetherConsolidatedFinancialStatementsArePreparedDEI': 'consolidated',
    }
    metadata = {}
    for node in root.iter():
        name = local_name(node.get('name') or str(node.tag))
        key = fields.get(name)
        value = ''.join(node.itertext()).strip()
        if key and value:
            if key in metadata and metadata[key] != value:
                raise ValueError('Conflicting fiscal/scope metadata in XBRL file')
            metadata[key] = value
    # Keep the issuer as part of context equality: same IDs from different
    # entities must never be silently combined.
    for node in root.iter():
        if local_name(str(node.tag)) != 'context':
            continue
        context = contexts.get(node.get('id'))
        if context is not None:
            context['identifiers'] = sorted({''.join(n.itertext()).strip() for n in node.iter()
                                             if local_name(str(n.tag)) == 'identifier'})
    return contexts, metadata


def package_resources(archive, names: list[str]) -> dict[str, tuple[dict, dict]]:
    """Summary and Attachment resources are independent context namespaces."""
    groups = {'summary': ({}, {}), 'attachment': ({}, {})}
    for name in names:
        group = 'summary' if '/summary/' in name.lower() else 'attachment'
        try:
            root = ET.fromstring(read_xbrl_bytes(archive.read(name)))
        except ET.ParseError:
            # An unreadable unrelated note must not suppress a valid Summary.
            # A missing resource still cannot authorize a new entity scope.
            continue
        contexts, metadata = resource_metadata(root)
        shared, shared_metadata = groups[group]
        for key, value in contexts.items():
            if key in shared and shared[key] != value:
                raise ValueError('Conflicting shared XBRL context: ' + key)
            shared[key] = value
        for key, value in metadata.items():
            if key in shared_metadata and shared_metadata[key] != value:
                raise ValueError('Conflicting shared XBRL metadata: ' + key)
            shared_metadata[key] = value
    return groups
