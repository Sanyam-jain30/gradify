import http
import json
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from dotenv import load_dotenv
import google.generativeai as genai
from PyPDF2 import PdfReader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts import PromptTemplate
from langchain.docstore.document import Document
import tempfile

# Initialize Flask app
app = Flask(__name__)
CORS(app)
# Load environment variables and configure Gemini
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=API_KEY)
#Load the Copyleaks API key
copyleaks_api_key = os.getenv("COPYLEAKS_API_KEY")

# Initialize variables
content_check_result = {}
visualization_data = []
percentage_grade = []
letter_grade = []

def convert_text_to_documents(text_chunks):
    return [Document(page_content=chunk) for chunk in text_chunks]

def get_pdf_text(pdf_docs):
    text = ""
    tasks = {}

    for pdf in pdf_docs:
        pdf_reader = PdfReader(pdf)
        for page in pdf_reader.pages:
            text += page.extract_text()
        tasks[pdf] = text
        
    return tasks

def get_text_chunks(text):
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=1000)
    chunks = text_splitter.split_text(text)
    return chunks

def get_vector_store(text_chunks):
    embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
    vector_store = FAISS.from_texts(text_chunks, embedding=embeddings)
    vector_store.save_local("faiss_index")

#Implementing the Copyleaks API
def create_scan_id(student_id):
    # Generate a submission_id with a shortened UUID
    uuid_part = str(uuid.uuid4()).replace('-', '')[:8]
    submission_id = f"submission{uuid_part}"
    
    # Combine the components
    scan_id = f"{student_id}-{submission_id}"
    
    # Ensure the scan_id meets the length requirement (3-36 characters)
    if len(scan_id) > 36:
        scan_id = scan_id[:36]
    
    return scan_id

def check_content_origin(text):
    conn = http.client.HTTPSConnection("api.copyleaks.com")

    login_token = os.getenv("COPYLEAKS_LOGIN_TOKEN")
    
    headers = {
        'Authorization': f"Bearer {login_token}",
        'Content-Type': "application/json",
        'Accept': "application/json"
    }
    
    payload = json.dumps({
        "text": text,
        "language": "en",
        "sandbox": False
    })

    try:
        scan_id = create_scan_id("studentid123")
        conn.request("POST", f"/v2/writer-detector/{scan_id}/check", body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        
        try:
            result = json.loads(data)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON response from API. Raw response: {data}"
        
        summary = result.get('summary', {})
        ai_score = summary.get('ai', 0)
        human_score = summary.get('human', 0)
        probability = summary.get('probability', 0.0)
        
        total_words = result.get('scannedDocument', {}).get('totalWords', 0)
        
        if ai_score > human_score:
            classification = "AI-generated content"
        elif human_score > ai_score:
            classification = "Human-generated content"
        else:
            classification = "Undetermined"

        
        return {
            "classification": classification,
            "ai_score": ai_score,
            "human_score": human_score,
            "total_words": total_words,
            "model_version": result.get('modelVersion', 'Unknown'),
        }
    
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        conn.close()



def get_gemini_response(image, prompt):
    model = genai.GenerativeModel("gemini-1.5-flash")  # Updated model
    response = model.generate_content([prompt, image[0]])
    return response.text


def input_image_setup(uploaded_file):
    if uploaded_file is not None:
        bytes_data = uploaded_file.getvalue()
        image_parts = [
            {
                "mime_type": uploaded_file.type,
                "data": bytes_data,
            }
        ]
        return image_parts
    else:
        return None

def get_rubric_chain():
    prompt_template = f"""
    Extract the given total points, criteria, and points/pts from the given rubric:\n {{context}}?\n

    Answer:
    """
    model = ChatGoogleGenerativeAI(model="gemini-pro", temperature=0.3)
    prompt = PromptTemplate(
        template=prompt_template, input_variables=["context"]
    )
    chain = load_qa_chain(model, chain_type="stuff", prompt=prompt)
    return chain

def get_conversational_chain(rubric=None):
    if rubric:
        rubric_text = f" according to the provided rubric:\n{{rubric}}. Strictly based on the grading criteria, total points, and the points for each criteria given in the provided rubric do the grading\n"
    else:
        rubric_text = " based on the general grading criteria.\n"
    
    prompt_template = f"""
    You are a trained expert on writing and literary analysis. Your job is to accurately and effectively grade a student's essay{rubric_text}
    Respond back with graded points and a level for each criteria. Don't rewrite the rubric. For each criteria, provide a brief comment (1-2 lines) explaining the score.
    In the end, write short feedback about what steps they might take to improve on their assignment. Write a total percentage grade and letter grade. In your overall response, try to be lenient and keep in mind that the student is still learning. While grading the essay remember the writing level the student is at while considering their course level, grade level, and the overall expectations of writing should be producing.
    Your grade should only be below 70 percent if the essay does not succeed at all in any of the criteria. Your grade should only be below 80 percent if the essay is not sufficient in most of the criteria. Your grade should only be below 90% if there are a few criteria where the essay doesn't excell. Your grade should only be above 90 percent if the essay succeeds in most of the criteria.
    Understand that the essay was written by a human and think about their writing expectations for their grade level/course level, be lenient and give the student the benefit of the doubt.

    Format each criteria exactly like this:
    • Criteria_name: score/total
      Brief comment explaining the score (1-2 lines maximum)

        Context:\n {{context}}?\n
        Question: \n{{question}}\n

    Answer: Get the answer in beautiful format, for the criteria present in rubric list them as specified above.
    """
    model = ChatGoogleGenerativeAI(model="gemini-pro", temperature=0.3)
    prompt = PromptTemplate(
        template=prompt_template, input_variables=["rubric", "context", "question"]
    )
    chain = load_qa_chain(model, chain_type="stuff", prompt=prompt)
    return chain

def extract_criteria_and_values(output_text):
    lines = output_text.split('\n')
    visualization_data.clear()  # Clear previous data

    for line in lines:
        line = line.strip()
        # Skip empty lines and lines with total/letter grade/feedback
        if not line or "Total Percentage Grade" in line or "Letter Grade" in line or "Feedback" in line:
            continue
            
        # Check if line starts with bullet point and contains a score
        if line.startswith('•') and '/' in line:
            # Extract criteria name (everything before the colon)
            criteria = line.split(':')[0].replace('•', '').strip()
            # Extract score (between colon and dash)
            score_part = line.split(':')[1].split('-')[0].strip()
            scored = score_part.split('/')[0].strip()
            total = score_part.split('/')[1].strip()
            
            visualization_data.append({
                "criteria": criteria,
                "scored": scored,
                "total": total
            })

def create_visualizations(output_text):
    global percentage_grade, letter_grade
    lines = output_text.split('\n')

    for line in lines:
        if "Total Percentage Grade" in line:
            percentage_grade.append(float(line.split(':')[1].strip().replace('%', '').replace('*', '').strip()))
        elif "Letter Grade" in line:
            letter_grade.append((':')[1].strip().replace('*', '').strip())

@app.route('/hello', methods=['GET'])
def hello():
    return jsonify({'message': 'Hello, World!'})

@app.route('/api/grade/pdf', methods=['POST', 'OPTIONS'])
def grade_pdf():
    try:
        if 'pdf' not in request.files:
            return jsonify({'error': 'No PDF file uploaded'}), 400
        
        pdf_file = request.files.getlist('pdf')
        rubric_file = request.files.get('rubric')
        question = request.form.get('question')
        
        if not question:
            return jsonify({'error': 'No question provided'}), 400

        temp_pdfs = []
        pdf_names = []

        for pdf in pdf_file:
            with tempfile.NamedTemporaryFile(delete=False) as temp_pdf:
                pdf.save(temp_pdf.name)
                temp_pdfs.append(temp_pdf.name)
                pdf_names.append(pdf.filename)
                temp_pdf.close()
        
        with tempfile.NamedTemporaryFile(delete=False) as temp_rubric:
            rubric_file.save(temp_rubric.name)
            temp_rubric.close()
            
        raw_text = get_pdf_text(temp_pdfs)
        responses = ""
        i = 0

        for key, value in raw_text.items():
            text_chunks = get_text_chunks(value)
            get_vector_store(text_chunks)
            rubric_text = get_pdf_text([temp_rubric.name]) if temp_rubric.name else None

            if rubric_text:
                for rubric_key in rubric_text:
                    rubric_str = rubric_text[rubric_key]

                rubric_chain = get_rubric_chain()

                response = rubric_chain({"input_documents": convert_text_to_documents([rubric_str])}, return_only_outputs=True)
                rubric_text = response["output_text"]
            
            chain = get_conversational_chain(rubric=rubric_text)
            
            documents = convert_text_to_documents(text_chunks)
            
            response = chain({"input_documents": documents, "rubric": rubric_text, "question": question}, return_only_outputs=True)
            responses += f"\nResponse for {pdf_names[i]}: \n\n" + response['output_text']
            
            create_visualizations(response["output_text"])
            extract_criteria_and_values(response["output_text"])
            i += 1
        
        for temp in temp_pdfs:
            os.unlink(temp)
        os.unlink(temp_rubric.name)
        
        return jsonify({
            'status': 'success',
            'response': responses
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/visualization', methods=['POST', 'OPTIONS'])
def visualization_pdf():
    try:       
        print(visualization_data)
        return json.dumps({"criteria": visualization_data, "percentage_grade": percentage_grade, "letter_grade": letter_grade})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/plagirism', methods=['POST', 'OPTIONS'])
def plagirism_check():
    global content_check_result
    try:       
        return json.dumps({"Classification": content_check_result['classification'], "AI Score": content_check_result['ai_score'], "Human Score": content_check_result['human_score']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/grade/image', methods=['POST', 'OPTIONS'])
def grade_image():
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No Image file uploaded'}), 400
        
        answer_files = request.files.getlist('image')
        question_file = request.files.get('rubric')
        
        if not question_file:
            return jsonify({'error': 'No question provided'}), 400

        temp_images = []
        pdf_names = []

        for image in answer_files:
            # Save the uploaded file temporarily
            with tempfile.NamedTemporaryFile(delete=False) as temp_image:
                image.save(temp_image.name)
                temp_images.append(temp_image.name)
                pdf_names.append(image.filename)
                temp_image.close()
        
        # Save the uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False) as temp_question:
            question_file.save(temp_question.name)
            temp_question.close()
            
        input_prompt = """
        Your task is to determine if the student's solution \
        is correct or not.
        To solve the problem do the following:
        - First, work out your own solution to the problem. 
        - Then compare your solution to the student's solution \ 
        and evaluate if the student's solution is correct or not. 
        Don't decide if the student's solution is correct until 
        you have done the problem yourself.
        Use the following format:
        Question:

        question here

        \n
        Student's solution:

        student's solution here

        \n
        Actual solution:

        steps to work out the solution and your solution here

        \n
        Is the student's solution the same as actual solution \
        just calculated:

        yes or no

        \n
        Student grade:
        ```
        correct or incorrect
        """
        ## If submit button is clicked

        solution_data = input_image_setup([temp_images]) if temp_images else None
        full_prompt = input_prompt.format(
            question_here="The question will be read from uploaded image.",
            student_solution_here="The solution will be read from uploaded image.",
            actual_solution_here="The actual solution will be calculated here."
        )
        response = get_gemini_response(solution_data, full_prompt)
            
        # Clean up temporary file
        for temp in temp_images:
            os.unlink(temp)
        os.unlink(temp_question.name)
        
        return jsonify({
            'status': 'success',
            'response': response
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=8080)