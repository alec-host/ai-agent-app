module.exports = {
  apps: [
    {
      name: "MATTER_MINER_AGENTIC_AI",
      // Path to the gunicorn executable inside your virtual environment
      script: "venv/bin/gunicorn",
      // cwd: "./src",	    
      // Arguments passed to gunicorn
      args: "-w 4 -k uvicorn.workers.UvicornWorker src.main:app --bind 0.0.0.0:8005",
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
