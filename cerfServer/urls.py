"""
URL configuration for cerfServer project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import Http404
from django.urls import path, include, re_path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def jwt_create_disabled(_request):
    raise Http404


urlpatterns = [
    path("admin/", admin.site.urls),

    # Block /auth/jwt/create, /auth/jwt/create/, and accidental prefix matches.
    # Must be before djoser.urls.jwt.
    re_path(r"^auth/jwt/create.*$", jwt_create_disabled),

    path("auth/", include("djoser.urls")),
    path("auth/", include("djoser.urls.jwt")),

    # Keep this after auth routes.
    path("", include("calibration.urls")),
]
