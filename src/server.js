import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import { GoogleGenerativeAI } from '@google/generative-ai';

// Carrega as variáveis do .env (se estiver rodando local)
dotenv.config();

const app = express();

// Permite que seu frontend (qualquer domínio) faça requisições para esta API
app.use(cors());
app.use(express.json());

const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

const systemInstruction = `
Você é a Inteligência Artificial de Atendimento e Vendas da "Sóstrass" (DAJP), um e-commerce líder no Brasil de materiais para artesanato.
Objetivo: Responder avaliações com foco em retenção, autoridade técnica e SEO.

REGRAS DE OURO:
1. Foco Estrito: Fale APENAS do produto comprado.
2. Especificações: A Caixa Organizadora tem 30 compartimentos e design slim (1,8cm). Nunca peça desculpas por ser "pequena", defenda que é compacta e segura.
3. Fiscal: Não gere crédito de ICMS.

Tom de voz:
- 4 ou 5 estrelas: Celebre, use palavras-chave do produto, instigue a recompra.
- 1 a 3 estrelas: Seja polida, defenda o produto tecnicamente. Se for logística, culpe a transportadora com educação.

Formato de saída OBRIGATÓRIO (JSON):
{
  "intencao": "elogio | reclamacao_logistica | reclamacao_produto | duvida",
  "sentimento": "positivo | neutro | negativo",
  "sugestao_autopilot": "responder_automatico | pausar_para_revisao_manual",
  "resposta_gerada": "Texto da resposta (max 450 char). Assine: Conte sempre com a Sóstrass! ✨"
}
`;

app.post('/api/generate', async (req, res) => {
  try {
    const { produto, estrelas, comentario } = req.body;

    if (!produto || !estrelas || !comentario) {
      return res.status(400).json({ error: "Faltam dados da avaliação" });
    }

    const model = genAI.getGenerativeModel({
      model: "gemini-2.5-pro",
      systemInstruction: systemInstruction,
      generationConfig: {
        responseMimeType: "application/json",
        temperature: 0.3,
      }
    });

    const userPrompt = `
      Analise e responda esta avaliação:
      - Produto Comprado: ${produto}
      - Estrelas: ${estrelas}
      - Comentário do Cliente: "${comentario}"
    `;

    const result = await model.generateContent(userPrompt);
    const responseText = result.response.text();
    
    const jsonResponse = JSON.parse(responseText);

    return res.status(200).json(jsonResponse);

  } catch (error) {
    console.error("Erro na API:", error);
    return res.status(500).json({ error: "Falha interna no motor de IA." });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`🚀 Motor da Sóstrass rodando na porta ${PORT}`);
});
