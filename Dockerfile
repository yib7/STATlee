# 1. Use an official, lightweight Python image as our base OS
FROM python:3.12-slim

# 2. Install R and necessary system libraries
# We pre-install R, dplyr, ggplot2, and MASS so the container is instantly ready
RUN apt-get update && apt-get install -y \
    r-base \
    r-cran-dplyr \
    r-cran-ggplot2 \
    r-cran-mass \
    && rm -rf /var/lib/apt/lists/*

# 3. Set the working directory inside the container
WORKDIR /app

# 4. Copy the requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of your application code (app.py, templates folder, etc.)
COPY . .

# 6. Expose the port Flask runs on
EXPOSE 5000

# 7. Define environment variables to tell Flask how to run
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# 8. The command to start the server when the container launches
CMD ["flask", "run"]