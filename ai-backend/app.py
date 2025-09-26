from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd
import sqlite3
import os
import subprocess
import time
import json
import threading
from datetime import datetime
import signal
import requests
from dotenv import load_dotenv
from config import Config

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app, origins=Config.CORS_ORIGINS)

# Ensure directories exist
Config.ensure_directories()

# Store running dashboards
running_dashboards = {}

class DashboardGenerator:
    def __init__(self):
        self.ollama_url = f"{Config.OLLAMA_URL}/api/generate"

    def convert_excel_to_sqlite(self, excel_path, db_path=None):
        """Convert Excel file to SQLite database"""
        if db_path is None:
            db_path = os.path.join(Config.DATA_DIR, 'terminal_data.db')

        try:
            # Read Excel file
            df = pd.read_excel(excel_path)

            # Create SQLite connection
            conn = sqlite3.connect(db_path)

            # Get table name from filename
            table_name = os.path.splitext(os.path.basename(excel_path))[0].lower().replace('-', '_').replace(' ', '_')

            # Store data in SQLite
            df.to_sql(table_name, conn, if_exists='replace', index=False)

            # Get column info for LLM context
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()

            conn.close()

            return {
                'success': True,
                'db_path': db_path,
                'table_name': table_name,
                'columns': [col[1] for col in columns],
                'sample_data': df.head(3).to_dict('records')
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def call_llm(self, prompt):
        """Call local Ollama LLM"""
        try:
            payload = {
                "model": Config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "top_p": 0.9
                }
            }

            response = requests.post(self.ollama_url, json=payload, timeout=Config.OLLAMA_TIMEOUT)
            if response.status_code == 200:
                return response.json().get('response', '')
            else:
                return None
        except Exception as e:
            print(f"LLM Error: {e}")
            return None

    def generate_dashboard_code(self, user_prompt, data_context):
        """Generate Streamlit dashboard code using LLM"""

        # Build context-rich prompt for LLM
        llm_prompt = f"""
You are an expert Python developer creating a Streamlit dashboard for Terminal Manager operations.

USER REQUEST: "{user_prompt}"

DATA CONTEXT:
- Database: terminal_data.db
- Table: {data_context['table_name']}
- Columns: {', '.join(data_context['columns'])}
- Sample Data: {json.dumps(data_context['sample_data'], indent=2)}

REQUIREMENTS:
1. Create a complete, executable Streamlit dashboard
2. Use Terminal Manager branding (blue/navy theme)
3. Include relevant metrics, charts, and visualizations
4. Use plotly for interactive charts
5. Make it professional and polished
6. Handle data gracefully (check for null values)

Generate ONLY the Python code for the Streamlit dashboard. Start directly with imports, no explanations.

```python
"""

        response = self.call_llm(llm_prompt)

        if response:
            # Extract Python code from response
            if '```python' in response:
                code = response.split('```python')[1].split('```')[0].strip()
            elif '```' in response:
                code = response.split('```')[1].strip()
            else:
                code = response.strip()

            return code

        # Fallback template if LLM fails
        return self.get_fallback_dashboard(data_context)

    def get_fallback_dashboard(self, data_context):
        """Fallback dashboard template"""
        return f"""
import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import numpy as np

# Page config
st.set_page_config(
    page_title="Terminal Manager Dashboard",
    page_icon="🏢",
    layout="wide"
)

# Custom CSS for Terminal Manager theme
st.markdown('''
<style>
    .main {{
        background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%);
        color: white;
    }}
    .metric-card {{
        background: white;
        color: #1e3a8a;
        padding: 1rem;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        margin: 0.5rem 0;
    }}
    .stPlotlyChart {{
        background: white;
        border-radius: 8px;
        padding: 1rem;
    }}
</style>
''', unsafe_allow_html=True)

st.title("🏢 Terminal Manager Analytics Dashboard")
st.markdown("---")

# Load data
@st.cache_data
def load_data():
    try:
        conn = sqlite3.connect('../../data/terminal_data.db')
        df = pd.read_sql_query("SELECT * FROM {data_context['table_name']}", conn)
        conn.close()
        return df
    except Exception as e:
        st.error(f"Error loading data: {{e}}")
        return pd.DataFrame()

df = load_data()

if not df.empty:
    # Key metrics
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.metric("Total Records", len(df))
        st.markdown('</div>', unsafe_allow_html=True)

    # Display data overview
    st.subheader("📊 Data Overview")
    st.dataframe(df.head(10), use_container_width=True)

    # Basic visualization
    if len(df.columns) >= 2:
        st.subheader("📈 Data Visualization")

        # Try to create a meaningful chart
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        categorical_cols = df.select_dtypes(include=['object']).columns.tolist()

        if len(numeric_cols) > 0 and len(categorical_cols) > 0:
            fig = px.bar(df, x=categorical_cols[0], y=numeric_cols[0],
                        title=f"{{numeric_cols[0]}} by {{categorical_cols[0]}}")
            st.plotly_chart(fig, use_container_width=True)

        elif len(numeric_cols) >= 2:
            fig = px.scatter(df, x=numeric_cols[0], y=numeric_cols[1],
                           title=f"{{numeric_cols[0]}} vs {{numeric_cols[1]}}")
            st.plotly_chart(fig, use_container_width=True)
else:
    st.error("No data available")
"""

    def create_dashboard_file(self, code, dashboard_id):
        """Create Streamlit dashboard file"""
        filename = f"dashboard_{dashboard_id}.py"
        filepath = os.path.join(Config.DASHBOARD_DIR, filename)

        with open(filepath, 'w') as f:
            f.write(code)

        return filepath

    def start_streamlit_dashboard(self, filepath, port):
        """Start Streamlit dashboard on specified port"""
        try:
            cmd = [
                'streamlit', 'run', filepath,
                '--server.port', str(port),
                '--server.headless', 'true',
                '--server.enableCORS', 'false',
                '--server.enableXsrfProtection', 'false'
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Config.DASHBOARD_DIR
            )

            # Wait a moment for Streamlit to start
            time.sleep(3)

            return process
        except Exception as e:
            print(f"Error starting Streamlit: {e}")
            return None

# Initialize generator
generator = DashboardGenerator()

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'AI Dashboard Backend Running'})

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get comprehensive system status"""
    status = Config.get_status()
    return jsonify(status)

@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    return jsonify({
        'ollama_url': Config.OLLAMA_URL,
        'model': Config.OLLAMA_MODEL,
        'port': Config.AI_BACKEND_PORT,
        'debug': Config.DEBUG,
        'streamlit_base_port': Config.STREAMLIT_BASE_PORT
    })

@app.route('/api/dashboard/generate', methods=['POST'])
def generate_dashboard():
    try:
        data = request.get_json()
        user_prompt = data.get('prompt', '')
        excel_path = data.get('excel_path', '')

        if not user_prompt:
            return jsonify({'error': 'Prompt is required'}), 400

        # Find available Excel files if not specified
        if not excel_path:
            excel_files = [f for f in os.listdir('..') if f.endswith('.xlsx')]
            if excel_files:
                excel_path = os.path.join('..', excel_files[0])
            else:
                return jsonify({'error': 'No Excel files found'}), 400

        # Convert Excel to SQLite
        data_context = generator.convert_excel_to_sqlite(excel_path)
        if not data_context['success']:
            return jsonify({'error': f"Data processing failed: {data_context['error']}"}), 500

        # Generate dashboard code
        dashboard_code = generator.generate_dashboard_code(user_prompt, data_context)

        # Create unique dashboard ID
        dashboard_id = f"{int(time.time())}_{hash(user_prompt) % 10000}"

        # Create dashboard file
        dashboard_path = generator.create_dashboard_file(dashboard_code, dashboard_id)

        # Find available port
        port = Config.STREAMLIT_BASE_PORT
        while port in [info['port'] for info in running_dashboards.values()]:
            port += 1

        # Start Streamlit dashboard
        process = generator.start_streamlit_dashboard(dashboard_path, port)

        if process:
            # Store dashboard info
            running_dashboards[dashboard_id] = {
                'process': process,
                'port': port,
                'created_at': datetime.now().isoformat(),
                'prompt': user_prompt,
                'file_path': dashboard_path
            }

            dashboard_url = f"http://localhost:{port}"

            return jsonify({
                'success': True,
                'dashboard_id': dashboard_id,
                'dashboard_url': dashboard_url,
                'embed_url': f"{dashboard_url}/?embed=true",
                'message': 'Dashboard generated successfully'
            })
        else:
            return jsonify({'error': 'Failed to start dashboard'}), 500

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/list', methods=['GET'])
def list_dashboards():
    dashboard_list = []
    for dashboard_id, info in running_dashboards.items():
        dashboard_list.append({
            'id': dashboard_id,
            'port': info['port'],
            'created_at': info['created_at'],
            'prompt': info['prompt'],
            'url': f"http://localhost:{info['port']}"
        })

    return jsonify({'dashboards': dashboard_list})

@app.route('/api/dashboard/stop/<dashboard_id>', methods=['POST'])
def stop_dashboard(dashboard_id):
    if dashboard_id in running_dashboards:
        process = running_dashboards[dashboard_id]['process']
        process.terminate()
        del running_dashboards[dashboard_id]
        return jsonify({'success': True, 'message': 'Dashboard stopped'})
    else:
        return jsonify({'error': 'Dashboard not found'}), 404

if __name__ == '__main__':
    print("🚀 AI Dashboard Backend starting...")
    print(f"🔧 Configuration: {Config.OLLAMA_URL} | Model: {Config.OLLAMA_MODEL}")
    print("📊 Ready to generate Streamlit dashboards!")

    # Test Ollama connection on startup
    status = Config.validate_ollama_connection()
    if status['connected']:
        print("✅ Ollama connected successfully")
    else:
        print(f"⚠️  Ollama connection failed: {status.get('error', 'Unknown error')}")
        print("💡 Start Ollama with: ollama serve")

    app.run(
        debug=Config.DEBUG,
        host=Config.AI_BACKEND_HOST,
        port=Config.AI_BACKEND_PORT
    )