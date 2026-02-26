from typing import Literal, Optional
from pydantic import Field
from datetime import datetime, date, time, timedelta
from .base import ToolBase

class DateTimeTool(ToolBase):
    """Access date and time information and perform datetime operations."""
    
    operation: Literal["current_datetime", "current_date", "current_time", "format", "parse", "difference"] = Field(
        description="Operation to perform: 'current_datetime' returns current datetime, 'current_date' returns current date, 'current_time' returns current time, 'format' formats a datetime string, 'parse' parses a datetime string, 'difference' calculates difference between two dates/times"
    )
    
    datetime_string: Optional[str] = Field(
        default=None,
        description="Datetime string for format/parse operations. For format: input datetime string. For parse: string to parse."
    )
    
    format_string: Optional[str] = Field(
        default=None,
        description="Format string for format operation (e.g., '%Y-%m-%d %H:%M:%S'). Uses Python's strftime/strptime format."
    )
    
    datetime_string_a: Optional[str] = Field(
        default=None,
        description="First datetime string for difference operation"
    )
    
    datetime_string_b: Optional[str] = Field(
        default=None,
        description="Second datetime string for difference operation"
    )
    
    def execute(self) -> str:
        try:
            if self.operation == "current_datetime":
                now = datetime.now()
                return f"Current datetime: {now.isoformat()}"
            
            elif self.operation == "current_date":
                today = date.today()
                return f"Current date: {today.isoformat()}"
            
            elif self.operation == "current_time":
                now = datetime.now()
                current_time = now.time()
                return f"Current time: {current_time.isoformat()}"
            
            elif self.operation == "format":
                if not self.datetime_string:
                    return "Error: datetime_string is required for format operation"
                if not self.format_string:
                    return "Error: format_string is required for format operation"
                
                # Try to parse the datetime string
                parsed_dt = self._parse_datetime(self.datetime_string)
                formatted = parsed_dt.strftime(self.format_string)
                return f"Formatted datetime: {formatted}"
            
            elif self.operation == "parse":
                if not self.datetime_string:
                    return "Error: datetime_string is required for parse operation"
                if not self.format_string:
                    return "Error: format_string is required for parse operation"
                
                parsed_dt = datetime.strptime(self.datetime_string, self.format_string)
                return f"Parsed datetime: {parsed_dt.isoformat()}"
            
            elif self.operation == "difference":
                if not self.datetime_string_a or not self.datetime_string_b:
                    return "Error: both datetime_string_a and datetime_string_b are required for difference operation"
                
                dt_a = self._parse_datetime(self.datetime_string_a)
                dt_b = self._parse_datetime(self.datetime_string_b)
                
                if dt_a > dt_b:
                    diff = dt_a - dt_b
                    direction = "later"
                else:
                    diff = dt_b - dt_a
                    direction = "earlier"
                
                # Return difference in various units
                days = diff.days
                seconds = diff.seconds
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                seconds = seconds % 60
                
                return f"Time difference: {days} days, {hours} hours, {minutes} minutes, {seconds} seconds ({dt_a.isoformat()} is {direction} than {dt_b.isoformat()})"
            
            else:
                return f"Unknown operation: {self.operation}"
                
        except Exception as e:
            return f"Error performing datetime operation: {e}"
    
    def _parse_datetime(self, dt_string: str) -> datetime:
        """Parse a datetime string using common formats."""
        # Try ISO format first
        try:
            return datetime.fromisoformat(dt_string)
        except ValueError:
            pass
        
        # Try common formats
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y",
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(dt_string, fmt)
            except ValueError:
                continue
        
        raise ValueError(f"Could not parse datetime string: {dt_string}. Supported formats: ISO format (YYYY-MM-DD[THH:MM:SS]), YYYY-MM-DD HH:MM:SS, YYYY-MM-DD HH:MM, YYYY-MM-DD, DD/MM/YYYY HH:MM:SS, DD/MM/YYYY HH:MM, DD/MM/YYYY, MM/DD/YYYY HH:MM:SS, MM/DD/YYYY HH:MM, MM/DD/YYYY")