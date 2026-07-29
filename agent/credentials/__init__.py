"""Credential injection subsystem for the agent."""

from .injector import CredentialError, Secret, CredentialInjector

__all__ = ["CredentialError", "Secret", "CredentialInjector"]
