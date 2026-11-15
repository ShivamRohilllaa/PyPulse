from django.contrib import admin
from .models import Audit, Exercise, Maintenance, Monitor, Service
from .forms import MonitorForm, ExerciseForm
admin.site.site_header="PyPulse administration"

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display=("name","framework","target","last_seen")
    def has_add_permission(self, request):
        return False

@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):
    form=ExerciseForm

@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    form=MonitorForm

admin.site.register(Maintenance)
@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
    list_display=("at","actor","action")
    def has_add_permission(self,request):
        return False
    def has_change_permission(self,request,obj=None):
        return False
    def has_delete_permission(self,request,obj=None):
        return False
