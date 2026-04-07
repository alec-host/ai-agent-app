module.exports = {
  apps: [
    {
      name: "MATTER_MINER_AGENTIC_AI",
       // Path to the uvicorn executable inside your virtual environment (Windows)
       script: "venv/Scripts/uvicorn.exe",
       // Arguments passed to uvicorn
       args: "src.main:app --host 0.0.0.0 --port 8005",
       // Ensure PM2 knows it's a direct binary execution
       interpreter: "none",
      env: {
        NODE_ENV: "production",
        PYTHONPATH: "."
      },
      // Optional: Logging configuration
      error_file: "./logs/err.log",
      out_file: "./logs/out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss"
    }
  ]
};
