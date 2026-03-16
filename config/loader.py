"""
Configuration loader for LLM provider configurations.
Supports JSON, YAML, and environment variable loading.
"""
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Union

from .models import LLMConfig, ProviderConfig, FallbackConfig, BudgetConfig

logger = logging.getLogger(__name__)

class ConfigLoader:
    """
    Load and validate LLM configurations from various sources.
    
    Features:
    - Load from JSON/YAML files
    - Environment variable substitution
    - Validation and schema checking
    - Default configuration generation
    """
    
    @staticmethod
    def load_from_file(filepath: Union[str, Path]) -> LLMConfig:
        """
        Load configuration from a JSON or YAML file.
        
        Args:
            filepath: Path to configuration file
            
        Returns:
            Validated LLMConfig instance
            
        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If configuration is invalid
        """
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Configuration file not found: {filepath}")
        
        # Determine file type and load
        if filepath.suffix.lower() in ['.json']:
            with open(filepath, 'r') as f:
                config_data = json.load(f)
        elif filepath.suffix.lower() in ['.yaml', '.yml']:
            try:
                import yaml
                with open(filepath, 'r') as f:
                    config_data = yaml.safe_load(f)
            except ImportError:
                raise ImportError(
                    "PyYAML package required for YAML configuration. "
                    "Install with: pip install pyyaml"
                )
        else:
            raise ValueError(f"Unsupported configuration file format: {filepath.suffix}")
        
        # Apply environment variable substitution
        config_data = ConfigLoader._substitute_env_vars(config_data)
        
        # Parse and validate
        return ConfigLoader.load_from_dict(config_data)
    
    @staticmethod
    def load_from_dict(config_dict: Dict[str, Any]) -> LLMConfig:
        """
        Load configuration from a dictionary.
        
        Args:
            config_dict: Configuration dictionary
            
        Returns:
            Validated LLMConfig instance
        """
        # Convert provider_type string to enum if needed
        if "primary_provider" in config_dict:
            provider_config = config_dict["primary_provider"]
            if isinstance(provider_config, dict) and "provider_type" in provider_config:
                provider_config["provider_type"] = provider_config["provider_type"].lower()
        
        # Handle fallback chain
        if "fallback_chain" in config_dict and config_dict["fallback_chain"]:
            fallback_config = config_dict["fallback_chain"]
            if isinstance(fallback_config, dict) and "providers" in fallback_config:
                for provider in fallback_config["providers"]:
                    if isinstance(provider, dict) and "provider_type" in provider:
                        provider["provider_type"] = provider["provider_type"].lower()
        
        # Create LLMConfig instance
        try:
            return LLMConfig(**config_dict)
        except Exception as e:
            raise ValueError(f"Invalid configuration: {e}")
    
    @staticmethod
    def load_from_env() -> Optional[LLMConfig]:
        """
        Load configuration from environment variables.
        
        Returns:
            LLMConfig instance or None if insufficient environment variables
        """
        # Check for minimal required environment variables
        provider_type = os.getenv("LLM_PROVIDER_TYPE")
        api_key = os.getenv("LLM_API_KEY")
        
        if not provider_type or not api_key:
            logger.debug("Insufficient environment variables for LLM configuration")
            return None
        
        # Build basic configuration
        config_dict = {
            "primary_provider": {
                "provider_type": provider_type.lower(),
                "api_key": api_key,
                "model": os.getenv("LLM_MODEL", ""),
                "base_url": os.getenv("LLM_BASE_URL"),
                "temperature": float(os.getenv("LLM_TEMPERATURE", "0.7")),
                "timeout": int(os.getenv("LLM_TIMEOUT", "120")),
                "max_retries": int(os.getenv("LLM_MAX_RETRIES", "3")),
            }
        }
        
        # Parse max_tokens if provided
        max_tokens = os.getenv("LLM_MAX_TOKENS")
        if max_tokens:
            config_dict["primary_provider"]["max_tokens"] = int(max_tokens)
        
        # Try to load from dictionary
        return ConfigLoader.load_from_dict(config_dict)
    
    @staticmethod
    def _substitute_env_vars(data: Any) -> Any:
        """
        Recursively substitute environment variable references in configuration.
        Supports ${VAR_NAME} syntax.
        
        Args:
            data: Configuration data (dict, list, or scalar)
            
        Returns:
            Data with environment variables substituted
        """
        if isinstance(data, dict):
            return {k: ConfigLoader._substitute_env_vars(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [ConfigLoader._substitute_env_vars(item) for item in data]
        elif isinstance(data, str) and data.startswith("${") and data.endswith("}"):
            # Environment variable substitution
            var_name = data[2:-1]
            value = os.getenv(var_name)
            if value is None:
                logger.warning(f"Environment variable {var_name} not found")
                return data  # Return original string
            return value
        else:
            return data
    
    @staticmethod
    def create_default_config() -> LLMConfig:
        """
        Create a default configuration with OpenAI-compatible provider.
        Useful for testing and development.
        
        Returns:
            Default LLMConfig instance
        """
        from .models import ProviderType
        
        default_provider = ProviderConfig(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key="sk-dummy-key-for-development-only",
            model="gpt-3.5-turbo",
            temperature=0.7,
            timeout=120,
            max_retries=3
        )
        
        return LLMConfig(
            primary_provider=default_provider,
            enable_cost_tracking=True,
            log_level="INFO"
        )
    
    @staticmethod
    def save_to_file(config: LLMConfig, filepath: Union[str, Path], format: str = "json"):
        """
        Save configuration to a file.
        
        Args:
            config: LLMConfig instance to save
            filepath: Path to save configuration
            format: Output format ("json" or "yaml")
            
        Raises:
            ValueError: If format is unsupported
        """
        filepath = Path(filepath)
        config_dict = config.dict(exclude_none=True)
        
        if format.lower() == "json":
            with open(filepath, 'w') as f:
                json.dump(config_dict, f, indent=2, default=str)
        elif format.lower() in ["yaml", "yml"]:
            try:
                import yaml
                with open(filepath, 'w') as f:
                    yaml.dump(config_dict, f, default_flow_style=False)
            except ImportError:
                raise ImportError(
                    "PyYAML package required for YAML export. "
                    "Install with: pip install pyyaml"
                )
        else:
            raise ValueError(f"Unsupported output format: {format}")
        
        logger.info(f"Configuration saved to {filepath}")