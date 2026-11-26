from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/status/', views.api_status, name='api_status'),
    path('api/pair/', views.api_pair, name='api_pair'),
    path('api/chats/', views.api_chats, name='api_chats'),
    path('api/convert/', views.api_convert, name='api_convert'),
    path('api/send/', views.api_send, name='api_send'),
    path('api/sounds/', views.api_sounds_list, name='api_sounds_list'),
    path('api/sounds/search/', views.api_sounds_search, name='api_sounds_search'),
    path('media/meme_sounds/<str:filename>', views.serve_meme_sound, name='serve_meme_sound'),
    path('api/logout/', views.api_logout, name='api_logout'),
]
