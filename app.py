import os
import tempfile
import json
import re
import subprocess
import math
import base64
import pandas as pd
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types


# ---------------------------------------------------------------------------
# INITIALIZATION & SETUP
# ---------------------------------------------------------------------------
load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("No GEMINI_API_KEY set for Flask application")

client = genai.Client(api_key=api_key)

app = Flask(__name__, template_folder='templates')
UPLOAD_FOLDER = tempfile.mkdtemp()
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB max upload

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    """Handles dataset uploads and generates a basic profile."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file and file.filename.endswith('.csv'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            df = pd.read_csv(filepath, nrows=100)
            profile = {
                'total_columns': len(df.columns),
                'numeric_columns': df.select_dtypes(include=['number']).columns.tolist(),
                'categorical_columns': df.select_dtypes(exclude=['number']).columns.tolist(),
                'headers': df.columns.tolist()
            }
            return jsonify({'message': 'File uploaded', 'filename': filename, 'profile': profile}), 200
        except Exception as e:
            return jsonify({'error': f"Error reading CSV: {str(e)}"}), 500
            
    return jsonify({'error': 'Invalid file format. Please upload a CSV.'}), 400


@app.route('/data_page', methods=['POST'])
def data_page():
    """Returns a specific 100-row page of the dataset with optional column filters applied."""
    data = request.json
    filename = data.get('filename')
    page = int(data.get('page', 1))
    per_page = int(data.get('per_page', 100))
    filters = data.get('filters', {}) 

    if not filename:
        return jsonify({'error': 'Missing filename'}), 400

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    try:
        df = pd.read_csv(filepath)
        
        for col, search_term in filters.items():
            if search_term and col in df.columns:
                df = df[df[col].astype(str).str.contains(str(search_term), case=False, na=False)]
        
        total_rows = len(df)
        total_pages = math.ceil(total_rows / per_page)
        
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        
        df_page = df.iloc[start_idx:end_idx].fillna("")
        
        return jsonify({
            'status': 'success',
            'data': df_page.to_dict(orient='records'),
            'total_rows': total_rows,
            'total_pages': total_pages,
            'current_page': page
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/classify_variables', methods=['POST'])
def classify_variables():
    """FEATURE: The Codebook - Intelligently classifies variables."""
    data = request.json
    filename = data.get('filename')
    
    if not filename: 
        return jsonify({'error': 'Missing filename'}), 400
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        # 1. Read a safe, fast sample
        df = pd.read_csv(filepath, nrows=100)
        
        # 2. Extract explicit column names so Pandas doesn't hide them!
        dtypes_dict = df.dtypes.astype(str).to_dict()
        
        # 3. Small preview just for AI context
        data_preview = df.head(20).to_string()
        
        prompt = f"""
        Analyze this dataset sample preview below. Your goal is to Classify EVERY column listed in the 'DATA PREVIEW' as 'Nominal', 'Ordinal', or 'Continuous'. 
        Return STRICTLY a JSON object mapping column names to their classification.
        Example: {{"Age": "Continuous", "Gender": "Nominal", "Education_Level": "Ordinal"}}
        
        DATA PREVIEW:
        {data_preview}
        """
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1, 
                response_mime_type="application/json"
            )
        )
        return jsonify({'status': 'success', 'codebook': json.loads(response.text)})
    except Exception as e:
        print(f"Classification Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/suggest', methods=['POST'])
def suggest_analysis():
    """Generates proactive analytical questions based on the uploaded data."""
    data = request.json
    filename = data.get('filename')
    
    if not filename: 
        return jsonify({'error': 'Missing filename'}), 400

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        df = pd.read_csv(filepath, nrows=20)
        data_preview = df.to_string()
        
        prompt = f"""
        You are a proactive data science mentor. Review this dataset sample:

        

        {data_preview}



        Suggest 3 specific, interesting analytical questions a social scientist could ask CodeCaster. 
        Return strictly as a JSON array of 3 strings. 
        Example: ["Query 1", "Query 2", "Query 3"]
        """
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7, 
                response_mime_type="application/json"
            )
        )
        return jsonify({'status': 'success', 'suggestions': json.loads(response.text)})
    except Exception as e:
         return jsonify({'error': str(e)}), 500


@app.route('/chat', methods=['POST'])
def chat():
    """
    CORE PIPELINE:
    1. Moderation Check (Is it safe/relevant?)
    2. Code Generation (Drafting based on Codebook)
    3. Code Validation (Reviewing the draft for execution errors)
    """
    data = request.json
    user_prompt = data.get('prompt')
    filename = data.get('filename')
    target_language = data.get('language', 'Python')
    history = data.get('history', [])
    codebook = data.get('codebook', {})

    if not user_prompt or not filename: 
        return jsonify({'error': 'Missing prompt or filename'}), 400

    moderation_prompt = f"""
    You are a strict safety and relevance filter for an academic data analysis tool.
    Assess the following user request: "{user_prompt}"
    
    Rules:
    1. If the request is asking to build malware, hack, or engage in illegal/unethical acts, reply STRICTLY with "BLOCK: Safety Violation".
    2. If the request is wildly off-topic (e.g., "write a poem about cats", "how do I bake a cake"), reply STRICTLY with "BLOCK: Off-topic".
    3. If the request is a valid data analysis, coding, or statistical query, reply STRICTLY with "PASS".
    """
    try:
        mod_response = client.models.generate_content(
            model='gemini-3.1-flash-lite-preview', 
            contents=moderation_prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        mod_result = mod_response.text.strip()
        
        if "BLOCK" in mod_result:
            return jsonify({'error': f"Request denied. {mod_result}"}), 403
            
    except Exception as e:
        return jsonify({'error': 'Moderation service failed.'}), 500

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        df = pd.read_csv(filepath, nrows=0)
        headers = df.columns.tolist()
    except Exception as e: 
        return jsonify({'error': 'Could not read dataset headers.'}), 500

    system_instruction = f"""
    You are CodeCaster, an expert social science data analyst. 
    Dataset: '{filename}'. Columns: {', '.join(headers)}.
    
    INTELLIGENT CODEBOOK CLASSIFICATION:
    {json.dumps(codebook)}
    *CRITICAL*: Use this codebook to ensure statistical validity. Do not run continuous models (like OLS) on Nominal data. Select the appropriate tests based on variable type.
    
    CONSTRAINTS:
    1. Output ONLY raw, executable {target_language} code. No markdown formatting (no ```).
    2. No explanations in the output.
    3. If you generate a plot or graph, you MUST save it locally as 'plot.png'. NEVER open interactive plot windows.
    """

    if target_language.lower() == 'python':
        system_instruction += "\nUse pandas, statsmodels, matplotlib/seaborn. print() important statistical summaries to stdout. Use plt.savefig('plot.png', bbox_inches='tight') for plots."
    else:
        system_instruction += "\nUse base R, dplyr, ggplot2. print() or summary() models to stdout. Use ggsave('plot.png') or standard png('plot.png') for plots."

    draft_prompt = system_instruction + "\n\n--- CONVERSATION HISTORY ---\n"
    for msg in history: 
        draft_prompt += f"{msg['role'].upper()}: {msg['text']}\n"
    draft_prompt += f"\n--- CURRENT REQUEST ---\n{user_prompt}\nDRAFT SCRIPT:"

    try:
        draft_response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=draft_prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        draft_code = draft_response.text.strip()
    except Exception as e: 
        return jsonify({'error': 'Failed to generate code draft.'}), 500

    validation_prompt = f"""
    You are a senior code reviewer. Review the following {target_language} script designed to analyze a dataset named '{filename}'.
    
    DRAFT CODE:
    {draft_code}
    
    TASKS:
    1. Ensure all necessary libraries are imported (e.g., pandas, statsmodels in Python).
    2. Ensure missing values (NaN/NA) are handled before running statistical models.
    3. Ensure the code will execute without throwing syntax or TypeErrors.
    4. Output ONLY the finalized, raw executable code. NO MARKDOWN (do not use ```). NO EXPLANATIONS.
    """
    
    try:
        val_response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=validation_prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        

        final_code = val_response.text.strip()

        # Robustly extract code from markdown fences
        match = re.search(r'```(?:python|r)?\n(.*?)```', final_code, re.DOTALL | re.IGNORECASE)
        if match:
            final_code = match.group(1).strip()
        elif final_code.startswith("```"):
            # Fallback: strip all lines that are just fence markers
            final_code = '\n'.join(
                line for line in final_code.split('\n')
                if not line.strip().startswith('```')
            )

        return jsonify({
            'status': 'success', 
            'language': target_language, 
            'code': final_code
        })
        
    except Exception as e: 
        return jsonify({'error': f'Code validation failed: {str(e)}'}), 500


@app.route('/run', methods=['POST'])
def run_code():
    """Executes the generated code locally ONLY."""
    data = request.json
    code = data.get('code')
    language = data.get('language')
    
    if not code: 
        return jsonify({'error': 'No code provided.'}), 400

    try:
        suffix = '.py' if language.lower() == 'python' else '.R'
        fd, path = tempfile.mkstemp(suffix=suffix, dir=app.config['UPLOAD_FOLDER'])
        
        with os.fdopen(fd, 'w') as f: 
            f.write(code)
            
        plot_file = os.path.join(app.config['UPLOAD_FOLDER'], 'plot.png')
        if os.path.exists(plot_file):
            os.remove(plot_file)
        
        cmd = ['python', path] if language.lower() == 'python' else ['Rscript', path]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=app.config['UPLOAD_FOLDER'],
            timeout=60  # ← add this
        )
        
        final_output = result.stdout
        if result.stderr: 
            final_output += f"\n--- Output/Warnings ---\n{result.stderr}"
            
        os.remove(path)
        
        plot_data = None
        if os.path.exists(plot_file):
            with open(plot_file, "rb") as image_file:
                plot_data = base64.b64encode(image_file.read()).decode('utf-8')
            os.remove(plot_file) 

        return jsonify({'output': final_output.strip(), 'plot': plot_data})

    except subprocess.TimeoutExpired: 
        return jsonify({'error': 'Execution timed out. Code took too long to run.'}), 500
    except Exception as e: 
        return jsonify({'error': str(e)}), 500


@app.route('/interpret', methods=['POST'])
def interpret_results():
    """Handles the AI interpretation step with a two-step generation and formatting pipeline."""
    data = request.json
    final_output = data.get('output', '')
    plot_base64 = data.get('plot') # Retrieve the plot image data

    interpretation = "No statistical output or plots generated to interpret."
    
    # Run interpretation if we have either text output OR a plot
    if final_output.strip() or plot_base64:
        
        # STEP 1: Generate the deep analysis using the Pro model
        interp_prompt = f"""
        You are a data mentor. A user ran an analysis script and got the following terminal results.
        
        TERMINAL OUTPUT:
        {final_output if final_output.strip() else "No terminal text output."}
        """
        
        if plot_base64:
            interp_prompt += "\n\nYou have also been provided with the generated plot. Please analyze the visual trends, distributions, or relationships shown in the graph in conjunction with the terminal output."

        interp_prompt += """
        
        Write a clear, plain-English summary of what this means for a social scientist. 
        Interpret p-values, correlations, trends, or any findings from these terminal results 
        and the provided graph (if any) that are relevant for a data analyst/social scientist to know. 
        Do not introduce yourself, go straight into summarizing and analyzing.
        """
        
        # Package the prompt. If there's an image, append it to the contents list.
        contents_payload = [interp_prompt]
        if plot_base64:
            try:
                image_bytes = base64.b64decode(plot_base64)
                contents_payload.append(
                    types.Part.from_bytes(data=image_bytes, mime_type='image/png')
                )
            except Exception as e:
                print(f"Error decoding image for AI: {e}")
        
        try:
            # First pass: Deep thinking and interpretation (Multimodal)
            ai_resp = client.models.generate_content(
                model='gemini-3.1-pro-preview',
                contents=contents_payload,
                config=types.GenerateContentConfig(temperature=0.3)
            )
            raw_interpretation = ai_resp.text.strip()
            
            # STEP 2: Strict Markdown formatting pass using Flash (Text-only formatting)
            format_prompt = f"""
            You are a strict text formatter. Take the following statistical interpretation and format it into beautiful, perfectly clean Markdown.
            
            CRITICAL FORMATTING RULES:
            1. Use standard Markdown headers (###) for distinct sections.
            2. Use simple bullet points (*) for lists and key findings.
            3. Fix any broken formatting, orphaned asterisks, or messy spacing.
            4. DO NOT change the content of the text unless there is an emoji or emoticon detected, then delete it.
            5. Include proper spacing when necessary to improve readability and prevent users from being overwhelmed. 
            6. Output ONLY the formatted Markdown.

            RAW INTERPRETATION:
            {raw_interpretation}
            """
            
            # Second pass: Fast, rigid formatting
            format_resp = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=format_prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            interpretation = format_resp.text.strip()
            
        except Exception as e:
            print(f"Interpretation Error: {e}")
            interpretation = "Failed to generate or format AI interpretation."

    return jsonify({'interpretation': interpretation})


@app.route('/converse', methods=['POST'])
def converse():
    """FEATURE: The Converse Tab - Contextual chat with a 3.1 Flash Lite formatting pass."""
    data = request.json
    message = data.get('message')
    history = data.get('history', [])
    context = data.get('context', '') 
    code = data.get('code', '') # Grab the generated code from the frontend

    # STEP 1: Deep Analysis Prompt (Pro Model)
    analysis_prompt = f"""
    You are CodeCaster, an academic mentor.
    The user is asking a question about their recent analysis.
    
    --- RECENT RESULTS CONTEXT ---
    {context}
    
    --- GENERATED SCRIPT ---
    {code if code else "No code generated yet."}
    
    Answer the user's question directly, clearly, and concisely based on their data and the specific code provided.
    
    --- CONVERSATION HISTORY ---
    """
    for msg in history: 
        analysis_prompt += f"{msg['role'].upper()}: {msg['text']}\n"
        
    analysis_prompt += f"\nUSER: {message}\nCODECASTER:"

    try:
        # First Pass: Generate the raw, highly intelligent answer
        raw_response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=analysis_prompt,
            config=types.GenerateContentConfig(temperature=0.6)
        )
        raw_answer = raw_response.text.strip()
        
        # STEP 2: Strict Formatting Pass (Flash Lite Model)
        format_prompt = f"""
        You are a strict text formatter. Take the following conversational response and format it into clean, simple Markdown.
        
        CRITICAL FORMATTING RULES:
        1. Keep the structure simple and straightforward. Do not over-structure with unnecessary headers.
        2. Use inline code blocks (`like this`) for variable names, functions, or small code snippets.
        3. Use standard code blocks (```) only if providing a multi-line code correction.
        4. Use simple bullet points ONLY if listing multiple distinct items or steps.
        5. DO NOT change the underlying meaning, tone, or advice.
        6. Output ONLY the formatted Markdown.

        RAW RESPONSE:
        {raw_answer}
        """
        
        format_response = client.models.generate_content(
            model='gemini-3.1-flash-lite-preview',
            contents=format_prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        formatted_answer = format_response.text.strip()
        
        return jsonify({'status': 'success', 'reply': formatted_answer})
        
    except Exception as e:
        print(f"Converse Error: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)