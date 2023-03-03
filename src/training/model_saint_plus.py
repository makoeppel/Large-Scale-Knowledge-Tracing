import torch
import torch.nn as nn
import torch.nn.functional as F


class FFN(nn.Module):
    def __init__(self, in_feat):
        super(FFN, self).__init__()
        self.linear1 = nn.Linear(in_feat, in_feat)
        self.linear2 = nn.Linear(in_feat, in_feat)

    def forward(self, x):
        out = F.relu(self.linear1(x))
        out = self.linear2(out)
        return out


class EncoderEmbedding(nn.Module):
    def __init__(self, n_exercises, n_categories, n_dims, seq_len):
        super(EncoderEmbedding, self).__init__()
        self.n_dims = n_dims
        self.seq_len = seq_len
        self.exercise_embed = nn.Embedding(n_exercises, n_dims)
        self.category_embed = nn.Embedding(n_categories, n_dims)
        self.position_embed = nn.Embedding(seq_len, n_dims)

    def forward(self, exercises, categories):
        e = self.exercise_embed(exercises)
        c = self.category_embed(categories)
        if exercises.is_cuda:
            seq = torch.arange(self.seq_len, device='cuda').unsqueeze(0)
        else:
            seq = torch.arange(self.seq_len).unsqueeze(0)
        p = self.position_embed(seq)
        return p + c + e


class DecoderEmbedding(nn.Module):
    def __init__(self, n_responses, n_dims, seq_len):
        super(DecoderEmbedding, self).__init__()
        self.n_dims = n_dims
        self.seq_len = seq_len
        self.response_embed = nn.Embedding(n_responses, n_dims)
        self.time_embed = nn.Linear(1, n_dims, bias=False)
        self.position_embed = nn.Embedding(seq_len, n_dims)

    def forward(self, responses):
        e = self.response_embed(responses)
        if responses.is_cuda:
            seq = torch.arange(self.seq_len, device='cuda').unsqueeze(0)
        else:
            seq = torch.arange(self.seq_len).unsqueeze(0)
        p = self.position_embed(seq)
        return p + e


class StackedNMultiHeadAttention(nn.Module):
    def __init__(self, n_stacks, n_dims, n_heads, seq_len, n_multihead=1, dropout=0.0):
        super(StackedNMultiHeadAttention, self).__init__()
        self.n_stacks = n_stacks
        self.n_multihead = n_multihead
        self.n_dims = n_dims
        self.norm_layers = nn.LayerNorm(n_dims)
        # n_stacks has n_multiheads each
        self.multihead_layers = nn.ModuleList(
            n_stacks * [nn.ModuleList(n_multihead * [nn.MultiheadAttention(embed_dim=n_dims,
                                                                           num_heads=n_heads,
                                                                           dropout=dropout), ]), ])
        self.ffn = nn.ModuleList(n_stacks * [FFN(n_dims)])
        self.mask = torch.triu(torch.ones(seq_len, seq_len),
                               diagonal=1).to(dtype=torch.bool)

    def forward(self, input_q, input_k, input_v, encoder_output=None, break_layer=None):
        for stack in range(self.n_stacks):
            for multihead in range(self.n_multihead):
                norm_q = self.norm_layers(input_q)
                norm_k = self.norm_layers(input_k)
                norm_v = self.norm_layers(input_v)
                if input_q.is_cuda:
                    attn_mask = self.mask.to('cuda')
                else:
                    attn_mask = self.mask
                heads_output, _ = self.multihead_layers[stack][multihead](query=norm_q.permute(1, 0, 2),
                                                                        key=norm_k.permute(
                                                                            1, 0, 2),
                                                                        value=norm_v.permute(
                                                                            1, 0, 2),
                                                                        attn_mask=attn_mask)
                heads_output = heads_output.permute(1, 0, 2)
                # assert encoder_output != None and break_layer is not None
                if encoder_output is not None and multihead == break_layer:
                    assert break_layer <= multihead, "break layer should be less than multihead layers and postive " \
                                                     "integer "
                    input_k = input_v = encoder_output
                    input_q = input_q + heads_output
                else:
                    input_q = input_q + heads_output
                    input_k = input_k + heads_output
                    input_v = input_v + heads_output
            last_norm = self.norm_layers(heads_output)
            ffn_output = self.ffn[stack](last_norm)
            ffn_output = ffn_output + heads_output
        # after loops = input_q = input_k = input_v
        return ffn_output


class SAINTPlus(nn.Module):
    def __init__(self, n_enc_stack, n_dec_stack, emb_dims, n_enc_head, n_dec_head, seq_len,
                 total_exe, total_cat, total_res, total_lt, total_rt, dropout=0.0):
        # n_encoder,n_detotal_responses,seq_len,max_time=300+1
        super().__init__()
        self.loss = nn.BCEWithLogitsLoss()
        self.encoder_layer = StackedNMultiHeadAttention(
            n_stacks=n_enc_stack,
            n_dims=emb_dims,
            n_heads=n_enc_head,
            seq_len=seq_len,
            n_multihead=1,
            dropout=dropout
        )

        self.decoder_layer = StackedNMultiHeadAttention(
            n_stacks=n_dec_stack,
            n_dims=emb_dims,
            n_heads=n_dec_head,
            seq_len=seq_len,
            n_multihead=2,
            dropout=dropout
        )

        self.encoder_embedding = EncoderEmbedding(
            n_exercises=total_exe, n_categories=total_cat,
            n_dims=emb_dims, seq_len=seq_len)

        self.decoder_embedding = DecoderEmbedding(n_responses=total_res, n_dims=emb_dims, seq_len=seq_len)
        self.elapsed_time = nn.Embedding(total_rt, emb_dims)
        self.lagged_time = nn.Embedding(total_lt, emb_dims)
        self.fc = nn.Linear(emb_dims, 1)

    def forward(self, in_ex, in_cat, in_in, in_rt, in_lt):
        enc = self.encoder_embedding(exercises=in_ex, categories=in_cat)
        dec = self.decoder_embedding(responses=in_in)
        # elapsed_time = in_rt.unsqueeze(-1).float()
        ela_time = self.elapsed_time(in_rt.long())
        # lagged_time = in_lt.unsqueeze(-1).float()
        lag_time = self.lagged_time(in_lt.long())
        dec = dec + ela_time + lag_time
        # this encoder
        encoder_output = self.encoder_layer(input_k=enc,
                                            input_q=enc,
                                            input_v=enc)
        # this is decoder
        decoder_output = self.decoder_layer(input_k=dec,
                                            input_q=dec,
                                            input_v=dec,
                                            encoder_output=encoder_output,
                                            break_layer=1)
        # fully connected layer
        out = self.fc(decoder_output)
        return out.squeeze()
