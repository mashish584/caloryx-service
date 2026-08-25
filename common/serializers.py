"""Response-only serializers describing shapes this service returns but never
validates as input (see common.exceptions for the envelope they document)."""
from __future__ import annotations

from rest_framework import serializers


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.DictField(required=False)
    requestId = serializers.CharField(required=False)


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()
