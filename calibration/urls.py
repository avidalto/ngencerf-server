from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

import calibration.views.calibration_import_export_views
import calibration.views.calibration_landing_views
import calibration.views.get_jobs_views
from calibration.views import calibration_formulation_views, calibration_tuning_views, calibration_gage_views, \
    calibration_optimization_views, calibration_run_views, calibration_plot_views, calibration_import_export_views, calibration_landing_views, \
    calibration_evaluation_views, calibration_forecast_views, calibration_regionalization_views, \
    calibration_verification_views, calibration_secondary_data_views, calibration_download_views, calibration_log_files_views, calibration_mfa_views

urlpatterns = [
    ##################################
    # Landing page
    ##################################
    path('calibration/create_calibration_run/', calibration_landing_views.create_calibration_run, name="createCalibrationRun"),
    path('calibration/create_and_run_validation/', calibration_landing_views.create_and_run_validation, name="createAndRunValidation"),
    path('calibration/create_and_run_forecast/', calibration_landing_views.create_and_run_forecast, name="createAndRunForecast"),
    path('calibration/create_and_run_hindcast/', calibration_landing_views.create_and_run_hindcast, name="createAndRunHndcast"),
    path('calibration/get_footer/', calibration_landing_views.get_footer, name="getFooter"),
    path('calibration/get_git_info/', calibration_landing_views.get_git_info, name="getGitInfo"),
    path('calibration/load_calibration_run/', calibration.views.calibration_import_export_views.load_calibration_run, name="loadCalibrationRun"),
    path('calibration/delete_jobs/', calibration_landing_views.delete_jobs, name="deleteJobs"),
    path('calibration/archive_jobs/', calibration_landing_views.archive_jobs, name="archiveJobs"),
    path('calibration/lock_jobs/', calibration_landing_views.lock_jobs, name="lockJobs"),
    path('calibration/clone_job/', calibration_landing_views.clone_job, name="cloneJob"),

    ##################################
    # Get Jobs
    ##################################
    path('calibration/get_calibration_jobs/', calibration.views.get_jobs_views.get_calibration_jobs, name="getCalibrationJobs"),
    path('calibration/get_calibration_jobs_for_evaluation/', calibration.views.get_jobs_views.get_calibration_jobs_for_evaluation,
         name="getCalibrationJobsForEvaluation"),
    path('calibration/get_calibration_jobs_for_forecast/', calibration.views.get_jobs_views.get_calibration_jobs_for_forecast,
         name="getCalibrationJobsForForecast"),
    path('calibration/get_validation_jobs/', calibration.views.get_jobs_views.get_validation_jobs, name="getValidationJobs"),
    path('calibration/get_forecast_jobs/', calibration.views.get_jobs_views.get_forecast_jobs, name="getForecastJobs"),
    path('calibration/get_verification_jobs/', calibration.views.get_jobs_views.get_verification_jobs, name="getVerificationJobs"),
    path('calibration/get_calibration_jobs_summary/', calibration.views.get_jobs_views.get_calibration_jobs_summary, name="getCalibrationJobsSummary"),
    path('calibration/get_forecast_jobs_for_verification/', calibration.views.get_jobs_views.get_forecast_jobs_for_verification,
         name="getForecastJobsForVerification"),
    path('calibration/get_calibration_gages/', calibration.views.get_jobs_views.get_calibration_gages, name="getCalibrationGages"),
    path('calibration/get_calibration_gages_for_forecast/', calibration.views.get_jobs_views.get_calibration_gages_for_forecast,
         name="getCalibrationGagesForForecast"),
    path('calibration/get_calibration_gages_for_evaluation/', calibration.views.get_jobs_views.get_calibration_gages_for_evaluation,
         name="getCalibrationGagesForEvaluation"),
    path('calibration/get_forecast_gages/', calibration.views.get_jobs_views.get_forecast_gages, name="getForecastGages"),
    path('calibration/get_forecast_gages_for_verification/', calibration.views.get_jobs_views.get_forecast_gages_for_verification,
         name="getForecastGagesForVerification"),
    path('calibration/get_verification_gages/', calibration.views.get_jobs_views.get_verification_gages, name="getVerificationGages"),

    ##################################
    # Gage tab
    ##################################
    path('calibration/get_gage/', calibration_gage_views.get_gage, name="getGage"),
    path('calibration/load_gage_tab/', calibration_gage_views.load_gage_tab, name="loadGageTab"),
    path('calibration/save_gage_tab/', calibration_gage_views.save_gage_tab, name="saveGageTab"),
    path('calibration/update_and_get_gage_status/', calibration_gage_views.update_and_get_gage_status, name="updateAndGetGageStatus"),

    ##################################
    # Plot Definitions tab
    ##################################
    path('calibration/get_plot_names/', calibration_plot_views.get_plot_names, name="getPlotNames"),
    path('calibration/get_plot_names_for_comparison/', calibration_plot_views.get_plot_names_for_comparison, name="getPlotNamesForComparison"),
    path('calibration/get_plot/', calibration_plot_views.get_plot, name="getPlot"),
    path('calibration/get_plots_for_comparison/', calibration_plot_views.get_plots_for_comparison, name="getPlotsForComparison"),

    ##################################
    # Formulation tab
    ##################################
    path('calibration/get_modules/', calibration_formulation_views.get_modules, name="getModules"),
    path('calibration/load_formulation_tab/', calibration_formulation_views.load_formulation_tab, name="loadFormulationTab"),
    path('calibration/save_formulation_tab/', calibration_formulation_views.save_formulation_tab, name="saveFormulationTab"),

    ##################################
    # Tuning tab
    ##################################
    path('calibration/load_tuning_tab/', calibration_tuning_views.load_tuning_tab, name="loadTuningTab"),
    path('calibration/save_tuning_tab/', calibration_tuning_views.save_tuning_tab, name="saveTuningTab"),
    path('calibration/upload_user_parameters/', calibration_tuning_views.upload_user_parameters, name="uploadUserParameters"),
    path('calibration/validate_parameters/', calibration_tuning_views.validate_parameters, name="validateParameters"),

    ##################################
    # Optimizations/Metrics tab
    ##################################
    path('calibration/load_optimization_tab/', calibration_optimization_views.load_optimization_tab, name="loadOptimizationTab"),
    path('calibration/save_optimization_tab/', calibration_optimization_views.save_optimization_tab, name="saveOptimizationTab"),

    ##################################
    # Run tab
    ##################################
    path('calibration/get_status/', calibration_run_views.get_status, name="getStatus"),
    path('calibration/get_status_for_comparison/', calibration_run_views.get_status_for_comparison, name="getStatusForComparison"),
    path('calibration/run_calibration/', calibration_run_views.run_calibration, name="runCalibration"),
    path('calibration/report_iteration/', calibration_run_views.report_iteration, name="reportIteration"),
    path('calibration/get_iteration/', calibration_run_views.get_iteration, name="getIteration"),
    path('calibration/cancel_job/', calibration_run_views.cancel_job, name="cancelJob"),
    path('calibration/calibration_job_slurm_callback/', calibration_run_views.calibration_job_slurm_callback, name="calibrationJobSlurmCallback"),
    path('calibration/validation_job_slurm_callback/', calibration_run_views.validation_job_slurm_callback, name="validationJobSlurmCallback"),
    path('calibration/cold_start_job_slurm_callback/', calibration_run_views.cold_start_job_slurm_callback, name="coldStartJobSlurmCallback"),
    path('calibration/forecast_job_slurm_callback/', calibration_run_views.forecast_job_slurm_callback, name="forecastJobSlurmCallback"),
    path('calibration/hindcast_job_slurm_callback/', calibration_run_views.hindcast_job_slurm_callback, name="hindcastJobSlurmCallback"),
    path('calibration/verification_job_slurm_callback/', calibration_run_views.verification_job_slurm_callback, name="verificationJobSlurmCallback"),

    ##################################
    # Evaluation
    ##################################
    path('calibration/get_calibration_data_by_iteration/', calibration_evaluation_views.get_calibration_data_by_iteration,
         name="getCalibrationDataByIteration"),
    path('calibration/get_log_names/', calibration_log_files_views.get_log_names, name="getLogNames"),
    path('calibration/get_log/', calibration_log_files_views.get_log, name="getLog"),
    path('calibration/get_log_status/', calibration_log_files_views.get_log_status, name="getLogStatus"),

    ##################################
    # Download
    ##################################
    path('calibration/get_calibration_job_zip/', calibration_download_views.get_calibration_job_zip, name="getCalibrationJobZip"),
    path('calibration/start_zip_for_calibration_job/', calibration_download_views.start_zip_for_calibration_job, name="startZipForCalibrationJob"),
    path('calibration/get_zip_status/', calibration_download_views.get_zip_status, name="getZipStatus"),
    path("calibration/get_calibration_zip_download_url/", calibration_download_views.get_calibration_zip_download_url,
         name="getCalibrationZipDownloadUrl"),

    ##################################
    # Forecast
    ##################################
    path('calibration/load_forecast_tab/', calibration_forecast_views.load_forecast_tab, name="loadForecastTab"),

    path('calibration/clone_and_run_forecast/', calibration_forecast_views.clone_and_run_forecast_job, name="cloneAndRunForecastJob"),
    path('calibration/clone_and_run_hindcast/', calibration_forecast_views.clone_and_run_hindcast_job, name="cloneAndRunHindcastJob"),
    path('calibration/get_forecast_timeseries_data/', calibration_forecast_views.get_forecast_timeseries_data, name="getForecastTimeseriesData"),
    path('calibration/delete_forecast_job/', calibration_forecast_views.delete_forecast_job, name="deleteForecastJob"),
    path('calibration/delete_hindcast_job/', calibration_forecast_views.delete_forecast_job, name="deleteHindcastJob"),
    path('calibration/get_cold_start_jobs_for_configuration/', calibration_forecast_views.get_cold_start_jobs_for_configuration, name="getColdStartJobsForConfiguration"),

    ##################################
    # Verification
    ##################################
    path('calibration/create_and_run_verification_job/', calibration_verification_views.create_and_run_verification_job,
         name="createVerificationJob"),
    path('calibration/get_verification_plot_names/', calibration_verification_views.get_verification_plot_names, name="getVerificationPlotNames"),
    path('calibration/get_verification_plot/', calibration_verification_views.get_verification_plot, name="getVerificationPlot"),
    path('calibration/delete_verification_job/', calibration_verification_views.delete_verification_job, name="deleteVerificationJob"),

    ##################################
    # SWE
    ##################################
    path('calibration/get_swe_images_by_date/', calibration_secondary_data_views.get_swe_images_by_date, name="getSweImagesByDate"),
    path('calibration/get_swe_timeseries_data/', calibration_secondary_data_views.get_swe_timeseries_data, name="getSweTimeseriesData"),
    path('calibration/get_soil_moisture_images_by_date/', calibration_secondary_data_views.get_soil_moisture_images_by_date,
         name="getSoilMoistureImagesByDate"),
    path('calibration/get_soil_moisture_timeseries_data/', calibration_secondary_data_views.get_soil_moisture_timeseries_data,
         name="getSoilMoistureTimeseriesData"),

    ##################################
    # Import/Export
    ##################################
    path('calibration/export/', calibration_import_export_views.export_job, name="export"),
    path('calibration/import/', calibration.views.calibration_landing_views.import_job, name="import"),

    ##################################
    # Regionalization
    ##################################
    path('calibration/get_regionalization_files_zip/', calibration_regionalization_views.get_regionalization_files_zip,
         name="getRegionalizationFilesZip"),

    ##################################
    # MFA
    ##################################
    path("auth/mfa/setup/", calibration_mfa_views.setup_mfa, name="setupMfa"),
    path("auth/mfa/setup/confirm/", calibration_mfa_views.confirm_setup_mfa, name="confirmSetupMfa"),
    path("auth/mfa/verify/", calibration_mfa_views.verify_mfa, name="verifyMfa"),
    path("auth/login/", calibration_mfa_views.login, name="login"),

    ##################################
    # Swagger - drf_spectacular
    ##################################
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    # Optional UI:
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),

    ##################################
    # Testing
    ##################################
    path('calibration/get_slurm_token/', calibration_run_views.get_slurm_token, name="getSlurmToken"),
    path('calibration/process_calibration_output/', calibration_run_views.process_calibration_output, name="processCalibrationOutput"),
    path('calibration/process_swe_timeseries/', calibration_run_views.process_swe_timeseries, name="processSweTimeseries"),

]
