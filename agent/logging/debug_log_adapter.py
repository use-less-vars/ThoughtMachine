#!/usr/bin/env python3
"""
Debug log adapter for analyzing and migrating log files.

This module provides utilities to:
1. Analyze existing log files and categorize events
2. Convert old log formats to new category-aware format
3. Generate reports on log category distribution
"""

import json
import os
import sys
from typing import Dict, List, Any, Optional
from enum import Enum
from collections import Counter

from . import LogEventType, LogCategory, EVENT_TYPE_TO_CATEGORY


class LogAnalyzer:
    """Analyze log files and categorize events."""
    
    def __init__(self, log_file_path: str):
        """
        Initialize analyzer with log file path.
        
        Args:
            log_file_path: Path to JSONL log file
        """
        self.log_file_path = log_file_path
        self.events: List[Dict[str, Any]] = []
        
    def load_events(self) -> List[Dict[str, Any]]:
        """
        Load all events from the log file.
        
        Returns:
            List of log events as dictionaries
        """
        events = []
        try:
            with open(self.log_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            event = json.loads(line)
                            events.append(event)
                        except json.JSONDecodeError as e:
                            print(f"Warning: Could not parse line: {e}", file=sys.stderr)
        except FileNotFoundError:
            print(f"Error: Log file not found: {self.log_file_path}", file=sys.stderr)
            return []
        
        self.events = events
        return events
    
    def categorize_events(self) -> Dict[LogCategory, List[Dict[str, Any]]]:
        """
        Categorize loaded events using EVENT_TYPE_TO_CATEGORY mapping.
        
        Returns:
            Dictionary mapping categories to lists of events
        """
        if not self.events:
            self.load_events()
            
        categorized: Dict[LogCategory, List[Dict[str, Any]]] = {
            category: [] for category in LogCategory
        }
        uncategorized = []
        
        for event in self.events:
            event_type_str = event.get('type')
            if not event_type_str:
                uncategorized.append(event)
                continue
                
            try:
                event_type = LogEventType(event_type_str)
                category = EVENT_TYPE_TO_CATEGORY.get(event_type)
                if category:
                    categorized[category].append(event)
                else:
                    uncategorized.append(event)
            except ValueError:
                # Unknown event type
                uncategorized.append(event)
        
        # Remove empty categories
        categorized = {k: v for k, v in categorized.items() if v}
        
        if uncategorized:
            categorized[LogCategory.SESSION] = categorized.get(LogCategory.SESSION, []) + uncategorized
        
        return categorized
    
    def generate_report(self) -> str:
        """
        Generate a human-readable report of log categories.
        
        Returns:
            Report string
        """
        categorized = self.categorize_events()
        total_events = sum(len(events) for events in categorized.values())
        
        report_lines = []
        report_lines.append(f"Log Analysis Report for: {self.log_file_path}")
        report_lines.append(f"Total events: {total_events}")
        report_lines.append("")
        report_lines.append("Category distribution:")
        report_lines.append("-" * 40)
        
        for category in sorted(categorized.keys(), key=lambda c: c.value):
            events = categorized[category]
            count = len(events)
            percentage = (count / total_events * 100) if total_events > 0 else 0
            report_lines.append(f"{category.value:<12} {count:>6} events ({percentage:5.1f}%)")
        
        # Event type breakdown within each category
        report_lines.append("")
        report_lines.append("Detailed breakdown:")
        report_lines.append("-" * 40)
        
        for category, events in sorted(categorized.items(), key=lambda x: x[0].value):
            if not events:
                continue
                
            report_lines.append(f"\n{category.value}:")
            type_counter = Counter(e.get('type', 'unknown') for e in events)
            for event_type, count in sorted(type_counter.items()):
                report_lines.append(f"  {event_type:<30} {count:>4}")
        
        return "\n".join(report_lines)


def analyze_log_file(log_file_path: str) -> None:
    """
    Convenience function to analyze a log file and print report.
    
    Args:
        log_file_path: Path to JSONL log file
    """
    analyzer = LogAnalyzer(log_file_path)
    print(analyzer.generate_report())


def migrate_log_categories(log_file_path: str, output_path: Optional[str] = None) -> None:
    """
    Add category field to existing log entries (creates new file).
    
    Args:
        log_file_path: Path to source JSONL log file
        output_path: Path for output file (default: original name with '_categorized' suffix)
    """
    if output_path is None:
        base, ext = os.path.splitext(log_file_path)
        output_path = f"{base}_categorized{ext}"
    
    analyzer = LogAnalyzer(log_file_path)
    events = analyzer.load_events()
    
    migrated_count = 0
    with open(output_path, 'w', encoding='utf-8') as f:
        for event in events:
            event_type_str = event.get('type')
            migrated_event = event.copy()
            
            if event_type_str:
                try:
                    event_type = LogEventType(event_type_str)
                    category = EVENT_TYPE_TO_CATEGORY.get(event_type)
                    if category:
                        migrated_event['category'] = category.value
                        migrated_count += 1
                except ValueError:
                    pass  # Keep unknown event types unchanged
            
            f.write(json.dumps(migrated_event) + '\n')
    
    print(f"Migrated {migrated_count}/{len(events)} events")
    print(f"Output written to: {output_path}")


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Analyze and migrate log files')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Analyze command
    analyze_parser = subparsers.add_parser('analyze', help='Analyze log file categories')
    analyze_parser.add_argument('log_file', help='Path to JSONL log file')
    
    # Migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Add category field to log entries')
    migrate_parser.add_argument('log_file', help='Path to JSONL log file')
    migrate_parser.add_argument('--output', '-o', help='Output file path (optional)')
    
    args = parser.parse_args()
    
    if args.command == 'analyze':
        analyze_log_file(args.log_file)
    elif args.command == 'migrate':
        migrate_log_categories(args.log_file, args.output)
    else:
        parser.print_help()